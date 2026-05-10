from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import BlockedUser, HiRequest, Message
from .serializers import BasicUserSerializer, HiRequestSerializer, MessageSerializer
from quests.utils import check_and_complete_quest


def _are_friends(user_a, user_b):
    return HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED,
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
        return Response(HiRequestSerializer(hi).data, status=status.HTTP_201_CREATED)


class AcceptHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        hi = get_object_or_404(HiRequest, pk=pk, receiver=request.user, status=HiRequest.Status.PENDING)
        hi.status = HiRequest.Status.ACCEPTED
        hi.save(update_fields=['status'])
        check_and_complete_quest(request.user, 'make_friend')
        check_and_complete_quest(hi.sender, 'make_friend')
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
        accepted = HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user) | Q(receiver=request.user)
        ).select_related('sender', 'receiver')

        friends = []
        for hi in accepted:
            friend = hi.receiver if hi.sender == request.user else hi.sender
            friends.append(friend)

        return Response(BasicUserSerializer(friends, many=True).data)


class MessagesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if not _are_friends(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)

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
        if not _are_friends(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)

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

        today_sent = Message.objects.filter(
            sender=request.user,
            created_at__date=timezone.localdate(),
        ).count()
        if today_sent >= 5:
            check_and_complete_quest(request.user, 'send_message')

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
            return Response({'detail': 'No friendship found.'}, status=status.HTTP_404_NOT_FOUND)
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

        # Remove any existing friendship or pending Hi requests in both directions
        HiRequest.objects.filter(
            Q(sender=request.user, receiver=other) | Q(sender=other, receiver=request.user)
        ).delete()

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
