from django.db.models import Count
from rest_framework import serializers
from .models import SavedStory, Story, StoryReaction


class StorySerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    user_reaction = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()

    class Meta:
        model = Story
        fields = [
            'id', 'author', 'content', 'image', 'latitude', 'longitude',
            'place_name', 'expires_at', 'created_at', 'likes_count',
            'is_expired', 'user_reaction', 'reactions', 'is_saved',
        ]
        read_only_fields = ['id', 'expires_at', 'created_at', 'likes_count']

    def get_author(self, obj):
        from django.utils import timezone
        u = obj.user
        return {
            'id': u.id,
            'username': u.username,
            'profile_picture': u.profile_picture.url if u.profile_picture else None,
        }

    def get_is_expired(self, obj):
        from django.utils import timezone
        return obj.expires_at <= timezone.now()

    def get_user_reaction(self, obj):
        request = self.context.get('request')
        if not request:
            return None
        reaction = obj.reactions.filter(user=request.user).first()
        return reaction.reaction if reaction else None

    def get_is_saved(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        return SavedStory.objects.filter(user=request.user, story=obj).exists()

    def get_reactions(self, obj):
        counts = {}
        for r in StoryReaction.objects.filter(story=obj).values('reaction').annotate(count=Count('reaction')):
            counts[r['reaction']] = r['count']
        return counts


class StoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Story
        fields = ['content', 'image', 'latitude', 'longitude', 'place_name']


class StoryReactionSerializer(serializers.Serializer):
    reaction = serializers.ChoiceField(choices=StoryReaction.ReactionType.choices)
