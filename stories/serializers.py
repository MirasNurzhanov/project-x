from django.db.models import Count
from rest_framework import serializers
from .models import SavedStory, Story, StoryReaction


class StorySerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    # Kept under the old key: clients read `image` whether the file went
    # through Django or straight to Cloudinary.
    image = serializers.SerializerMethodField()
    # DRF's DecimalField stringifies by default (COERCE_DECIMAL_TO_STRING),
    # which Fabric's typed view-manager props on Android refuse outright —
    # AIRMapMarker throws "cannot be cast from String to double" rather than
    # coercing the way the old bridge did. Floats here so the fix holds
    # however the client normalises coordinates, not just for the one
    # currently in services/coords.ts.
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
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

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None

    def get_latitude(self, obj):
        return float(obj.latitude)

    def get_longitude(self, obj):
        return float(obj.longitude)

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
        # Walk the prefetched rows — `.filter()` here would bypass the
        # prefetch cache and issue one query per story in a list response.
        for r in obj.reactions.all():
            if r.user_id == request.user.pk:
                return r.reaction
        return None

    def get_is_saved(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        # List views hand in the whole set at once; the per-object query only
        # remains for single-story responses.
        saved_ids = self.context.get('saved_ids')
        if saved_ids is not None:
            return obj.pk in saved_ids
        return SavedStory.objects.filter(user=request.user, story=obj).exists()

    def get_reactions(self, obj):
        counts = {}
        for r in obj.reactions.all():
            counts[r.reaction] = counts.get(r.reaction, 0) + 1
        return counts


class StoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Story
        fields = ['content', 'image', 'image_url', 'latitude', 'longitude', 'place_name']


class StoryReactionSerializer(serializers.Serializer):
    reaction = serializers.ChoiceField(choices=StoryReaction.ReactionType.choices)
