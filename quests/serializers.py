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


class LeaderboardEntrySerializer(serializers.ModelSerializer):
    rank = serializers.SerializerMethodField()
    username = serializers.CharField(source='user.username')
    profile_picture = serializers.SerializerMethodField()
    latitude = serializers.DecimalField(source='user.latitude', max_digits=9, decimal_places=6, allow_null=True)
    longitude = serializers.DecimalField(source='user.longitude', max_digits=9, decimal_places=6, allow_null=True)

    class Meta:
        model = UserAuraPoints
        fields = ('rank', 'username', 'profile_picture', 'total_points', 'latitude', 'longitude')

    def get_rank(self, obj):
        return self.context['ranks'].get(obj.pk)

    def get_profile_picture(self, obj):
        pic = obj.user.profile_picture
        return pic.url if pic else None
