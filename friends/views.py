from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import BlockedUser, FriendRequest, HiRequest, Message
from .serializers import BasicUserSerializer, FriendRequestSerializer, HiRequestSerializer, MessageSerializer
from quests.utils import check_and_complete_quest
from notifications.utils import send_push_notification


def _are_connected(user_a, user_b):
    if BlockedUser.objects.filter(
        Q(blocker=user_a, blocked=user_b) | Q(blocker=user_b, blocked=user_a)
    ).exists():
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

    def post(self, request):
        receiver_id = request.data.get('receiver_id')
        if not receiver_id:
            return Response({'detail': 'receiver_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        receiver = get_object_or_404(User, pk=receiver_id)
        if receiver == request.user:
            return Response({'detail': 'You cannot send a Hi to yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        if BlockedUser.objects.filter(
            Q(blocker=receiver, blocked=request.user) | Q(blocker=request.user, blocked=receiver)
        ).exists():
            return Response({'detail': 'Cannot send Hi to this user.'}, status=status.HTTP_403_FORBIDDEN)

        HiRequest.objects.filter(sender=request.user, receiver=receiver).delete()

        hi = HiRequest.objects.create(sender=request.user, receiver=receiver)
        check_and_complete_quest(request.user, 'send_hi')
        send_push_notification(receiver, 'New Hi! 👋', f'@{request.user.username} wants to connect')
        return Response(HiRequestSerializer(hi).data, status=status.HTTP_201_CREATED)


class AcceptHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        hi = get_object_or_404(HiRequest, pk=pk, receiver=request.user, status=HiRequest.Status.PENDING)
        hi.status = HiRequest.Status.ACCEPTED
        hi.save(update_fields=['status'])
        check_and_complete_quest(request.user, 'make_friend')
        check_and_complete_quest(hi.sender, 'make_friend')
        send_push_notification(hi.sender, '🎉 Connected!', f'@{request.user.username} accepted your Hi')
        return Response(HiRequestSerializer(hi).data)


class DeclineHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        hi = get_object_or_404(HiRequest, pk=pk, receiver=request.user, status=HiRequest.Status.PENDING)
        hi.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class IncomingHiRequestsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        requests = HiRequest.objects.filter(
            receiver=request.user, status=HiRequest.Status.PENDING
        ).select_related('sender')
        return Response(HiRequestSerializer(requests, many=True).data)


class FriendsListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Users I blocked → appear in Blocked tab only, not in connections/friends
        i_blocked_ids = set(
            BlockedUser.objects.filter(blocker=request.user).values_list('blocked_id', flat=True)
        )
        # Users who blocked me → shown in connections/friends with blocked_by=True
        blocking_me_ids = set(
            BlockedUser.objects.filter(blocked=request.user).values_list('blocker_id', flat=True)
        )

        accepted_hi = HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user) | Q(receiver=request.user)
        ).select_related('sender', 'receiver')

        connections = []
        for hi in accepted_hi:
            other = hi.receiver if hi.sender == request.user else hi.sender
            if other.id not in i_blocked_ids:
                connections.append(other)

        accepted_fr = FriendRequest.objects.filter(
            status=FriendRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user) | Q(receiver=request.user)
        ).select_related('sender', 'receiver')

        friends = []
        for fr in accepted_fr:
            other = fr.receiver if fr.sender == request.user else fr.sender
            if other.id not in i_blocked_ids:
                friends.append(other)

        # Latest message and unread count per counterpart, for chat-list previews
        other_ids = {u.id for u in connections} | {u.id for u in friends}

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
                    receiver=request.user, is_read=False, sender_id__in=other_ids
                )
                .values('sender_id')
                .annotate(unread_count=Count('id'))
            )
            unread_map = {r['sender_id']: r['unread_count'] for r in unread_rows}

        def serialize_user(user):
            data = dict(BasicUserSerializer(user).data)
            data['blocked_by'] = user.id in blocking_me_ids
            last = last_by_user.get(user.id)
            if last is not None:
                data['last_message'] = last.content if last.content else '📷 Photo'
                data['last_message_at'] = last.created_at.isoformat()
            data['unread_count'] = unread_map.get(user.id, 0)
            return data

        return Response({
            'connections': [serialize_user(u) for u in connections],
            'friends':     [serialize_user(u) for u in friends],
        })


class MessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if not _are_connected(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)
        if BlockedUser.objects.filter(blocker=other, blocked=request.user).exists():
            return Response({'detail': 'Cannot receive messages from this user.'}, status=status.HTTP_403_FORBIDDEN)

        messages = Message.objects.filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).exclude(
            Q(sender=request.user, deleted_for_sender=True) |
            Q(receiver=request.user, deleted_for_receiver=True)
        ).order_by('created_at')

        Message.objects.filter(sender=other, receiver=request.user, is_read=False).update(is_read=True)

        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if not _are_connected(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)

        if BlockedUser.objects.filter(blocker=other, blocked=request.user).exists():
            return Response({'detail': 'Cannot send messages to this user.'}, status=status.HTTP_403_FORBIDDEN)

        content = request.data.get('content', '').strip()
        image = request.FILES.get('image')

        if not content and not image:
            return Response({'detail': 'content or image is required.'}, status=status.HTTP_400_BAD_REQUEST)

        message = Message.objects.create(
            sender=request.user,
            receiver=other,
            content=content,
            image=image,
        )

        check_and_complete_quest(request.user, 'send_message')
        preview = (content[:100] if content else '[image]')
        send_push_notification(other, '💬 New message', f'@{request.user.username}: {preview}')

        room_group = f"chat_{min(request.user.pk, other.pk)}_{max(request.user.pk, other.pk)}"
        async_to_sync(get_channel_layer().group_send)(
            room_group,
            {
                'type': 'chat.message',
                'id': message.id,
                'sender_id': request.user.pk,
                'content': message.content,
                'image_url': message.image.url if message.image else None,
                'created_at': message.created_at.isoformat(),
            },
        )

        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class RemoveFriendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
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
        Message.objects.filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()
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

    def post(self, request, user_id):
        receiver = get_object_or_404(User, pk=user_id)
        if receiver == request.user:
            return Response({'detail': 'You cannot send a friend request to yourself.'}, status=status.HTTP_400_BAD_REQUEST)

        if not _are_connected(request.user, receiver):
            return Response({'detail': 'You must be connected with this user first.'}, status=status.HTTP_403_FORBIDDEN)

        if BlockedUser.objects.filter(
            Q(blocker=receiver, blocked=request.user) | Q(blocker=request.user, blocked=receiver)
        ).exists():
            return Response({'detail': 'Cannot send a friend request to this user.'}, status=status.HTTP_403_FORBIDDEN)

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
            send_push_notification(receiver, '🎉 New friend!', f'@{request.user.username} is now your friend')
            return Response(FriendRequestSerializer(mutual).data, status=status.HTTP_201_CREATED)

        FriendRequest.objects.filter(sender=request.user, receiver=receiver).delete()
        fr = FriendRequest.objects.create(sender=request.user, receiver=receiver)
        send_push_notification(receiver, '👥 Friend request', f'@{request.user.username} wants to be friends')
        return Response(FriendRequestSerializer(fr).data, status=status.HTTP_201_CREATED)


class AcceptFriendRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        fr = get_object_or_404(FriendRequest, pk=pk, receiver=request.user, status=FriendRequest.Status.PENDING)
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
        send_push_notification(fr.sender, '🎉 New friend!', f'@{request.user.username} is now your friend')
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
        ).select_related('sender')
        return Response(FriendRequestSerializer(reqs, many=True).data)


class UnreadBreakdownView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        rows = (
            Message.objects.filter(receiver=request.user, is_read=False)
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
        count = Message.objects.filter(receiver=request.user, is_read=False).count()
        return Response({'unread_count': count})
