from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from friends.models import FriendRequest
from proximity.utils import haversine
from users.models import User
from .models import Quest, UserAuraPoints, UserQuest
from .serializers import QuestSerializer
from .utils import _is_reset_due


class QuestListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        quests = Quest.objects.all()
        user_quests = {
            uq.quest_id: uq
            for uq in UserQuest.objects.filter(user=request.user, quest__in=quests)
        }
        aura = UserAuraPoints.objects.filter(user=request.user).first()
        serializer = QuestSerializer(quests, many=True, context={'user_quests': user_quests})
        return Response({
            'total_points': aura.total_points if aura else 0,
            'quests': serializer.data,
        })


class CompleteQuestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            quest = Quest.objects.get(pk=pk)
        except Quest.DoesNotExist:
            return Response({'detail': 'Quest not found.'}, status=status.HTTP_404_NOT_FOUND)

        user_quest, _ = UserQuest.objects.get_or_create(user=request.user, quest=quest)

        if user_quest.completed and not _is_reset_due(user_quest):
            return Response(
                {'detail': 'Quest already completed for this period.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        user_quest.completed = True
        user_quest.completed_at = now
        user_quest.reset_date = timezone.localdate()
        user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])

        aura, _ = UserAuraPoints.objects.get_or_create(user=request.user)
        aura.total_points += quest.reward_points
        aura.save(update_fields=['total_points'])

        return Response({
            'detail': 'Quest completed.',
            'reward_points': quest.reward_points,
            'total_points': aura.total_points,
        }, status=status.HTTP_200_OK)


MEETUP_RADIUS_KM = 0.1  # 100 m


def _complete_quest_for_user(user, quest, now):
    user_quest, _ = UserQuest.objects.get_or_create(user=user, quest=quest)
    if user_quest.completed and not _is_reset_due(user_quest):
        return False
    user_quest.completed = True
    user_quest.completed_at = now
    user_quest.reset_date = timezone.localdate()
    user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])
    aura, _ = UserAuraPoints.objects.get_or_create(user=user)
    aura.total_points += quest.reward_points
    aura.save(update_fields=['total_points'])
    return True


class VerifyMeetupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            quest = Quest.objects.get(pk=pk)
        except Quest.DoesNotExist:
            return Response({'detail': 'Quest not found.'}, status=status.HTTP_404_NOT_FOUND)

        if quest.condition_type != Quest.ConditionType.MEET_FRIEND_IN_PERSON:
            return Response(
                {'detail': 'This quest does not support meetup verification.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        friend_id = request.data.get('friend_id')
        if not friend_id:
            return Response({'detail': 'friend_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            friend = User.objects.get(pk=friend_id)
        except User.DoesNotExist:
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

        is_friends = FriendRequest.objects.filter(
            status=FriendRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=request.user, receiver=friend) | Q(sender=friend, receiver=request.user)
        ).exists()
        if not is_friends:
            return Response(
                {'detail': 'You are not friends with this user.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        user = request.user
        if None in (user.latitude, user.longitude, friend.latitude, friend.longitude):
            return Response(
                {'detail': 'Both users must have their location set.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dist_km = haversine(
            float(user.latitude), float(user.longitude),
            float(friend.latitude), float(friend.longitude),
        )
        if dist_km > MEETUP_RADIUS_KM:
            return Response(
                {'detail': f'Too far apart ({round(dist_km * 1000)}m). Must be within 100m.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        completed_for_requester = _complete_quest_for_user(user, quest, now)
        completed_for_friend = _complete_quest_for_user(friend, quest, now)

        return Response({
            'detail': 'Meetup verified.',
            'quest': quest.title,
            'reward_points': quest.reward_points,
            'distance_m': round(dist_km * 1000),
            'completed_for_you': completed_for_requester,
            'completed_for_friend': completed_for_friend,
        }, status=status.HTTP_200_OK)
