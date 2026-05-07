from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User
from .models import BlockedUser, HiRequest, Message
from .serializers import BasicUserSerializer, HiRequestSerializer, MessageSerializer


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

        if HiRequest.objects.filter(sender=request.user, receiver=receiver).exists():
            return Response({'detail': 'Hi request already sent.'}, status=status.HTTP_400_BAD_REQUEST)

        hi = HiRequest.objects.create(sender=request.user, receiver=receiver)
        return Response(HiRequestSerializer(hi).data, status=status.HTTP_201_CREATED)


class AcceptHiView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        hi = get_object_or_404(HiRequest, pk=pk, receiver=request.user, status=HiRequest.Status.PENDING)
        hi.status = HiRequest.Status.ACCEPTED
        hi.save(update_fields=['status'])
        return Response(HiRequestSerializer(hi).data)


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
        ).order_by('created_at')

        Message.objects.filter(sender=other, receiver=request.user, is_read=False).update(is_read=True)

        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request, user_id):
        other = get_object_or_404(User, pk=user_id)
        if not _are_friends(request.user, other):
            return Response({'detail': 'You are not friends with this user.'}, status=status.HTTP_403_FORBIDDEN)

        content = request.data.get('content', '').strip()
        if not content:
            return Response({'detail': 'content is required.'}, status=status.HTTP_400_BAD_REQUEST)

        message = Message.objects.create(sender=request.user, receiver=other, content=content)

        room_group = f"chat_{min(request.user.pk, other.pk)}_{max(request.user.pk, other.pk)}"
        async_to_sync(get_channel_layer().group_send)(
            room_group,
            {
                'type': 'chat.message',
                'id': message.id,
                'sender_id': request.user.pk,
                'content': message.content,
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
