from rest_framework import serializers
from .models import Quest, UserAuraPoints


class QuestSerializer(serializers.ModelSerializer):
    completed = serializers.SerializerMethodField()
    completed_at = serializers.SerializerMethodField()

    class Meta:
        model = Quest
        fields = [
            'id', 'title', 'description', 'reward_points',
            'quest_type', 'condition_type', 'interest_id', 'completed', 'completed_at',
        ]

    def get_completed(self, obj):
        uq = self.context.get('user_quests', {}).get(obj.pk)
        return uq.completed if uq else False

    def get_completed_at(self, obj):
        uq = self.context.get('user_quests', {}).get(obj.pk)
        return uq.completed_at if uq else None


class UserAuraPointsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAuraPoints
        fields = ['total_points']
