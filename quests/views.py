from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

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
