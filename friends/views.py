import json

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from moderation import services as moderation_services
from moderation.models import ContentReport
from moderation.reasons import Reason
from rest_framework import permissions, serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users import visibility
from users.models import User
from .models import BlockedUser, FriendRequest, HiRequest, Message
from .lifecycle import lock_participants, lock_writable_conversation
from .serializers import BasicUserSerializer, FriendRequestSerializer, HiRequestSerializer, MessageSerializer
from .serializers import MessageHistoryQuerySerializer, MessageReadSerializer
from notifications.utils import (
    notify_direct_message, notify_story_reply, send_push_notification,
)
from stories.models import Story

# Story replies arrive as ordinary messages whose body is an encoded payload the
# two apps round-trip through us. The server only needs the human comment out of
# it — for the alert text and the push preview, both of which would otherwise
# show the raw blob.
STORY_REPLY_PREFIX = '§story-reply§'


def _story_reply_comment(content):
    """The human comment inside a story-reply payload, or `content` unchanged."""
    if not content or not content.startswith(STORY_REPLY_PREFIX):
        return content
    try:
        payload = json.loads(content[len(STORY_REPLY_PREFIX):])
    except (ValueError, TypeError):
        return ''
    comment = payload.get('comment') if isinstance(payload, dict) else None
    return comment if isinstance(comment, str) else ''


def _replied_to_story(story_id, author):
    """The story this message answers, or None. Only `author`'s own stories
    qualify: a client naming someone else's story must not be able to post an
    alert onto an unrelated author's feed."""
    if not story_id:
        return None
    try:
        story_id = int(story_id)
    except (TypeError, ValueError):
        return None
    return Story.objects.filter(pk=story_id, user=author).first()


def _blocked_either_way(user_a, user_b):
    return BlockedUser.objects.filter(
        Q(blocker=user_a, blocked=user_b) | Q(blocker=user_b, blocked=user_a)
    ).exists()


def _blocked_ids_for(user):
    """All user ids involved in a block with `user`, in either direction.
    Blocking is mutually invisible, so both sides get the same treatment.
    Thin alias so there is exactly one definition of "blocked"."""
    return visibility.blocked_ids_for(user)


def _are_connected(user_a, user_b):
    if not user_a.is_active or not user_b.is_active:
        return False
    if _blocked_either_way(user_a, user_b):
        return False
    return HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED,
    ).filter(
        Q(sender=user_a, receiver=user_b) | Q(sender=user_b, receiver=user_a)
    ).exists()


def _are_friends(user_a, user_b):
    return FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED,
    ).filter(
        Q(sender=user_a, receiver=user_b) | Q(sender=user_b, receiver=user_a)
    ).exists()


class SendHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'social_request'

    def post(self, request):
        receiver_id = request.data.get('receiver_id')
        if not receiver_id:
            return Response({'detail': 'receiver_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            receiver = visibility.visible_users(request.user).filter(pk=receiver_id).first()
        except (TypeError, ValueError, OverflowError):
            receiver = None
        if receiver is None:
            return visibility.not_found()

        # The existing unique constraint arbitrates concurrent first sends.
        # Repeats preserve pending, accepted and rejected rows and never re-push.
        with transaction.atomic():
            hi, created = HiRequest.objects.get_or_create(sender=request.user, receiver=receiver)
            if created:
                transaction.on_commit(lambda: send_push_notification(
                    receiver, 'New Hi! 👋', f'@{request.user.username} wants to connect',
                    data={'type': 'request', 'user_id': request.user.pk,
                          'username': request.user.username},
                    category='social',
                ))
        return Response(HiRequestSerializer(hi).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AcceptHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        hi = get_object_or_404(HiRequest, pk=pk, receiver=request.user, status=HiRequest.Status.PENDING)
        if _blocked_either_way(request.user, hi.sender):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not HiRequest.objects.filter(pk=hi.pk, status=HiRequest.Status.PENDING).update(
                status=HiRequest.Status.ACCEPTED):
            return visibility.not_found()
        hi.status = HiRequest.Status.ACCEPTED
        send_push_notification(
            hi.sender, '🎉 Connected!', f'@{request.user.username} accepted your Hi',
            data={'type': 'message', 'user_id': request.user.pk,
                  'username': request.user.username},
            category='social',
        )
        return Response(HiRequestSerializer(hi).data)


class DeclineHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        if not HiRequest.objects.filter(pk=pk, receiver=request.user,
                                        status=HiRequest.Status.PENDING).update(
                status=HiRequest.Status.REJECTED):
            return visibility.not_found()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IncomingHiRequestsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        requests = HiRequest.objects.filter(
            receiver=request.user, status=HiRequest.Status.PENDING
        ).exclude(
            sender_id__in=_blocked_ids_for(request.user)
        ).select_related('sender')
        return Response(HiRequestSerializer(requests, many=True).data)


class FriendsListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Blocking is mutually invisible: users I blocked AND users who blocked
        # me disappear from connections/friends (and with them, the chat
        # thread). No flag reveals which side placed the block.
        hidden_ids = _blocked_ids_for(request.user)

        accepted_hi = HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user) | Q(receiver=request.user)
        ).select_related('sender', 'receiver')

        connections = []
        for hi in accepted_hi:
            other = hi.receiver if hi.sender == request.user else hi.sender
            if other.id not in hidden_ids:
                connections.append(other)

        accepted_fr = FriendRequest.objects.filter(
            status=FriendRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user) | Q(receiver=request.user)
        ).select_related('sender', 'receiver')

        friends = []
        for fr in accepted_fr:
            other = fr.receiver if fr.sender == request.user else fr.sender
            if other.id not in hidden_ids:
                friends.append(other)

        # Latest message and unread count per counterpart, for chat-list previews
        # Retained history remains reachable after a connection is removed or
        # the counterpart requests deletion; it grants no relationship rights.
        connections = [u for u in connections if u.is_active]
        friends = [u for u in friends if u.is_active]
        other_ids = {u.id for u in connections} | {u.id for u in friends}
        history_ids = set(Message.objects.filter(sender=request.user, deleted_for_sender=False)
                          .values_list('receiver_id', flat=True).distinct())
        history_ids.update(Message.objects.filter(receiver=request.user, deleted_for_receiver=False)
                           .values_list('sender_id', flat=True).distinct())
        archived = list(User.objects.filter(pk__in=history_ids - other_ids - hidden_ids))
        other_ids.update(u.id for u in archived)

        last_by_user = {}
        if other_ids:
            msgs = Message.objects.filter(
                Q(sender=request.user, receiver_id__in=other_ids) |
                Q(sender_id__in=other_ids, receiver=request.user)
            ).exclude(
                Q(sender=request.user, deleted_for_sender=True) |
                Q(receiver=request.user, deleted_for_receiver=True)
            ).order_by('-created_at').only(
                'sender_id', 'receiver_id', 'content', 'image', 'created_at'
            )
            for m in msgs:
                other_id = m.receiver_id if m.sender_id == request.user.id else m.sender_id
                if other_id not in last_by_user:
                    last_by_user[other_id] = m
                    if len(last_by_user) == len(other_ids):
                        break

        unread_map = {}
        if other_ids:
            unread_rows = (
                Message.objects.filter(
                    receiver=request.user, is_read=False, deleted_for_receiver=False, sender_id__in=other_ids
                )
                .values('sender_id')
                .annotate(unread_count=Count('id'))
            )
            unread_map = {r['sender_id']: r['unread_count'] for r in unread_rows}

        def serialize_user(user, rel):
            data = dict(BasicUserSerializer(user).data)
            last = last_by_user.get(user.id)
            if last is not None:
                data['last_message'] = last.content if last.content else '📷 Photo'
                data['last_message_at'] = last.created_at.isoformat()
            data['unread_count'] = unread_map.get(user.id, 0)
            # The tier is already known from which list we are building, so it
            # is passed in rather than re-derived per user — presence_for would
            # otherwise run a relationship query per row.
            presence = visibility.presence_for(request.user, user, rel)
            if presence is not None:
                data['presence'] = presence
            return data

        return Response({
            'connections': [serialize_user(u, visibility.CONNECTED) for u in connections],
            'friends':     [serialize_user(u, visibility.FRIEND) for u in friends],
            'archived':    [dict(serialize_user(u, visibility.STRANGER), archived=True) for u in archived],
        })


class MessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, user_id):
        other = get_object_or_404(User, Q(pk=user_id) | Q(retired_account_id=user_id))
        if _blocked_either_way(request.user, other):
            return Response({'detail': 'Cannot receive messages from this user.'}, status=status.HTTP_403_FORBIDDEN)

        messages = Message.objects.filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).exclude(
            Q(sender=request.user, deleted_for_sender=True) |
            Q(receiver=request.user, deleted_for_receiver=True)
        ).select_related('sender').order_by('-id')
        connected = _are_connected(request.user, other)
        if not connected and not messages.exists():
            return Response({'detail': 'No retained conversation.'}, status=status.HTTP_403_FORBIDDEN)
        query = MessageHistoryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        before = query.validated_data.get('before')
        if before is not None:
            messages = messages.filter(id__lt=before)
        limit = query.validated_data['limit']
        page = list(messages[:limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        return Response({
            'results': MessageSerializer(list(reversed(page)), many=True).data,
            'next_before': page[-1].id if has_more else None,
            'has_more': has_more,
            'can_send': connected,
        })

    def post(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if not _are_connected(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)

        if BlockedUser.objects.filter(blocker=other, blocked=request.user).exists():
            return Response({'detail': 'Cannot send messages to this user.'}, status=status.HTTP_403_FORBIDDEN)

        if 'image_url' in request.data:
            return Response({'detail': 'Upload images using the image file field.',
                             'image_url': ['Legacy image URLs are read-only.']}, status=400)
        client_id = serializers.UUIDField(allow_null=True).run_validation(request.data.get('client_id'))
        if client_id is not None:
            existing = Message.objects.filter(sender=request.user, receiver=other, client_id=client_id).first()
            if existing is not None:
                if existing.deleted_for_sender or existing.deleted_for_receiver:
                    return visibility.not_found()
                return Response(MessageSerializer(existing).data)

        content = request.data.get('content', '').strip()
        image = request.FILES.get('image')
        if image is not None:
            from core.uploads import validate_image
            image = validate_image(image)

        if not content and not image:
            return Response({'detail': 'content or image is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # A direct message has exactly one reader, who already has Block,
        # Report and Delete - so unlike a story or a club message it is not
        # quarantined on a flag, and a moderator does not read it unprompted.
        # What is refused is the block tier: threats and material involving
        # minors do not become acceptable for being sent privately.
        verdict = moderation_services.screen(content, raise_on_block=False)
        if verdict.blocked:
            return Response(
                {'detail': verdict.message(), 'code': 'objectionable_content'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if client_id is not None and image:
            return Response({'detail': 'client_id is supported for text sends only.'}, status=400)
        with transaction.atomic():
            # Screening may await external work. Re-check standing after it,
            # serialized with removal/deletion, before creating any message.
            if not lock_writable_conversation(request.user.pk, other.pk):
                return Response({'detail': 'This conversation is no longer writable.'}, status=403)
            if client_id is not None:
                message, created = Message.objects.get_or_create(
                    sender=request.user, receiver=other, client_id=client_id,
                    defaults={'content': content},
                )
                if not created:
                    if message.deleted_for_sender or message.deleted_for_receiver:
                        return visibility.not_found()
                    return Response(MessageSerializer(message).data)
            else:
                message = Message.objects.create(
                    sender=request.user, receiver=other, content=content, image=image,
                )

        # A story reply is a normal DM carrying an encoded payload, plus a
        # `story_id` naming what it answers. It gets a story alert instead of the
        # generic message push — one interaction, one notification, and the
        # author can trace it back to the story rather than reading the raw
        # payload as a message preview.
        story = _replied_to_story(request.data.get('story_id'), other)
        if story is not None:
            notify_story_reply(story, request.user, _story_reply_comment(content))
        else:
            notify_direct_message(
                request.user, other, _story_reply_comment(content), image=image,
            )

        room_group = f"chat_{min(request.user.pk, other.pk)}_{max(request.user.pk, other.pk)}"
        async_to_sync(get_channel_layer().group_send)(
            room_group,
            {
                'type': 'chat.message',
                'id': message.id,
                'client_id': str(message.client_id) if message.client_id else None,
                'sender_id': request.user.pk,
                'content': message.content,
                'image_url': message.image_url or (message.image.url if message.image else None),
                'created_at': message.created_at.isoformat(),
            },
        )

        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class ReadMessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        other = get_object_or_404(User, Q(pk=user_id) | Q(retired_account_id=user_id))
        if _blocked_either_way(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=403)
        payload = MessageReadSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        through_id = payload.validated_data['through_id']
        # A real, visible inbound message anchors the acknowledgement to this
        # conversation. Newer messages arriving during a fetch remain unread.
        anchor = Message.objects.filter(
            pk=through_id, sender=other, receiver=request.user, deleted_for_receiver=False,
        ).exists()
        if not anchor:
            return visibility.not_found()
        updated = Message.objects.filter(
            sender=other, receiver=request.user, id__lte=through_id,
            deleted_for_receiver=False, is_read=False,
        ).update(is_read=True)
        return Response({'read_through': through_id, 'updated': updated})


class RemoveFriendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def delete(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        lock_participants(request.user.pk, other.pk)
        deleted, _ = HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()
        if not deleted:
            return Response({'detail': 'No connection found.'}, status=status.HTTP_404_NOT_FOUND)
        FriendRequest.objects.filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()
        Message.objects.filter(sender=request.user, receiver=other).update(deleted_for_sender=True)
        Message.objects.filter(sender=other, receiver=request.user).update(deleted_for_receiver=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, message_id):
        message = get_object_or_404(
            Message,
            pk=message_id,
        )
        if request.user not in (message.sender, message.receiver):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        scope = request.data.get('scope')
        if scope not in ('me', 'everyone'):
            return Response(
                {'detail': "scope must be 'me' or 'everyone'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if scope == 'everyone':
            if message.sender != request.user:
                return Response(
                    {'detail': 'Only the sender can delete for everyone.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            age_seconds = (timezone.now() - message.created_at).total_seconds()
            if age_seconds > 120:
                return Response(
                    {'detail': 'Message can only be deleted for everyone within 2 minutes of sending.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            message.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        # scope == 'me'
        if message.sender == request.user:
            message.deleted_for_sender = True
        else:
            message.deleted_for_receiver = True
        message.save(update_fields=['deleted_for_sender', 'deleted_for_receiver'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class BlockUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if other == request.user:
            return Response({'detail': 'You cannot block yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        _, created = BlockedUser.objects.get_or_create(blocker=request.user, blocked=other)
        if not created:
            return Response({'detail': 'User is already blocked.'}, status=status.HTTP_400_BAD_REQUEST)

        # Pending requests between the pair make no sense once a block exists —
        # drop them both ways. Accepted rows stay: the relationship is
        # suspended by the visibility filters and comes back on unblock.
        HiRequest.objects.filter(
            status=HiRequest.Status.PENDING,
        ).filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()
        FriendRequest.objects.filter(
            status=FriendRequest.Status.PENDING,
        ).filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()

        # App Review asked that blocking notify us, so a block raises a report.
        # Two things keep it from drowning the queue:
        #
        #   * `source=BLOCK` marks it as the weaker signal it is, so a moderator
        #     can tell it from somebody who pressed Report and chose a category.
        #   * `file_report` is idempotent per (reporter, target), so blocking
        #     and unblocking the same account repeatedly files one row.
        #
        # `reason` comes from the client when the block was raised from an
        # abusive-content context - a Swift's overflow menu, a profile - and
        # falls back to OTHER for an ordinary "I don't want to see this person"
        # block from the chat list, which is not an accusation of anything.
        reason = str(request.data.get('reason') or '').strip()
        if reason not in Reason.values:
            reason = Reason.OTHER
        moderation_services.report_user(
            request.user, other,
            reason=reason,
            note=str(request.data.get('note') or '')[:500],
            source=ContentReport.Source.BLOCK,
        )

        return Response(status=status.HTTP_204_NO_CONTENT)


class BlockedUsersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        entries = BlockedUser.objects.filter(blocker=request.user).select_related('blocked')
        return Response(BasicUserSerializer([e.blocked for e in entries], many=True).data)


class UnblockUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        deleted, _ = BlockedUser.objects.filter(blocker=request.user, blocked=other).delete()
        if not deleted:
            return Response({'detail': 'User is not blocked.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SendFriendRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'social_request'

    def post(self, request, user_id):
        receiver = get_object_or_404(User, pk=user_id)
        if receiver == request.user:
            return Response({'detail': 'You cannot send a friend request to yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        if _blocked_either_way(request.user, receiver):
            return Response({'detail': 'Cannot send a friend request to this user.'}, status=status.HTTP_403_FORBIDDEN)

        if not _are_connected(request.user, receiver):
            return Response({'detail': 'You must be connected with this user first.'}, status=status.HTTP_403_FORBIDDEN)

        if _are_friends(request.user, receiver):
            return Response({'detail': 'You are already friends with this user.'}, status=status.HTTP_400_BAD_REQUEST)

        # If receiver already sent a pending request to us, accept it — no manual step needed
        mutual = FriendRequest.objects.filter(
            sender=receiver, receiver=request.user, status=FriendRequest.Status.PENDING
        ).first()
        if mutual:
            mutual.status = FriendRequest.Status.ACCEPTED
            mutual.save(update_fields=['status'])
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"user_{request.user.pk}",
                {'type': 'new_friend', 'username': receiver.username},
            )
            async_to_sync(channel_layer.group_send)(
                f"user_{receiver.pk}",
                {'type': 'new_friend', 'username': request.user.username},
            )
            send_push_notification(
                receiver, '🎉 New friend!', f'@{request.user.username} is now your friend',
                data={'type': 'profile', 'user_id': request.user.pk,
                      'username': request.user.username},
                category='social',
            )
            return Response(FriendRequestSerializer(mutual).data, status=status.HTTP_201_CREATED)

        FriendRequest.objects.filter(sender=request.user, receiver=receiver).delete()
        fr = FriendRequest.objects.create(sender=request.user, receiver=receiver)
        send_push_notification(
            receiver, '👥 Friend request', f'@{request.user.username} wants to be friends',
            data={'type': 'request', 'user_id': request.user.pk,
                  'username': request.user.username},
            category='social',
        )
        return Response(FriendRequestSerializer(fr).data, status=status.HTTP_201_CREATED)


class AcceptFriendRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        fr = get_object_or_404(FriendRequest, pk=pk, receiver=request.user, status=FriendRequest.Status.PENDING)
        if _blocked_either_way(request.user, fr.sender):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        fr.status = FriendRequest.Status.ACCEPTED
        fr.save(update_fields=['status'])
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"user_{fr.sender.pk}",
            {'type': 'new_friend', 'username': request.user.username},
        )
        async_to_sync(channel_layer.group_send)(
            f"user_{request.user.pk}",
            {'type': 'new_friend', 'username': fr.sender.username},
        )
        send_push_notification(
            fr.sender, '🎉 New friend!', f'@{request.user.username} is now your friend',
            data={'type': 'profile', 'user_id': request.user.pk,
                  'username': request.user.username},
            category='social',
        )
        return Response(FriendRequestSerializer(fr).data)


class DeclineFriendRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        fr = get_object_or_404(FriendRequest, pk=pk, receiver=request.user, status=FriendRequest.Status.PENDING)
        fr.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IncomingFriendRequestsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        reqs = FriendRequest.objects.filter(
            receiver=request.user, status=FriendRequest.Status.PENDING
        ).exclude(
            sender_id__in=_blocked_ids_for(request.user)
        ).select_related('sender')
        return Response(FriendRequestSerializer(reqs, many=True).data)


class UnreadBreakdownView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rows = (
            Message.objects.filter(receiver=request.user, is_read=False)
            .exclude(sender_id__in=_blocked_ids_for(request.user))
            .values('sender__id', 'sender__username')
            .annotate(unread_count=Count('id'))
        )
        return Response([
            {
                'user_id': row['sender__id'],
                'username': row['sender__username'],
                'unread_count': row['unread_count'],
            }
            for row in rows
        ])


class UnreadMessageCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Message.objects.filter(
            receiver=request.user, is_read=False
        ).exclude(
            sender_id__in=_blocked_ids_for(request.user)
        ).count()
        return Response({'unread_count': count})
