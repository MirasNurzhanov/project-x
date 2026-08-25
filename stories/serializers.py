from django.db.models import Count
from moderation.drf import ScreenedTextMixin
from rest_framework import serializers
from .models import Story, StoryReaction
from .visibility import is_placed_for


class StorySerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    # Kept under the old key: clients read `image` whether the file went
    # through Django or (for stories from before the R2 migration) straight to
    # Cloudinary.
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
    is_starred = serializers.SerializerMethodField()

    class Meta:
        model = Story
        fields = [
            'id', 'author', 'content', 'image', 'latitude', 'longitude',
            'place_name', 'expires_at', 'created_at', 'likes_count',
            'is_expired', 'user_reaction', 'reactions', 'is_starred',
        ]
        read_only_fields = ['id', 'expires_at', 'created_at', 'likes_count']

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None

    def get_latitude(self, obj):
        # None for a story posted while location sharing was paused. Those
        # carry no position at all - see stories/models.Story.latitude.
        return None if obj.latitude is None else float(obj.latitude)

    def get_longitude(self, obj):
        return None if obj.longitude is None else float(obj.longitude)

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

    def get_is_starred(self, obj):
        # Only the author's own view of a story carries a meaningful star —
        # `is_starred` records that *they* favorited *their own* post, which
        # is not information anyone else reading the story needs to see.
        request = self.context.get('request')
        return bool(request and request.user.pk == obj.user_id and obj.is_starred)

    def get_reactions(self, obj):
        counts = {}
        for r in obj.reactions.all():
            counts[r.reaction] = counts.get(r.reaction, 0) + 1
        return counts


class SocialStorySerializer(StorySerializer):
    """A story as the chat/avatar surfaces need it: no coordinates.

    Those surfaces answer "what did my friend post", never "where". They were
    served by the map serializer, which meant a friend opening a story ring
    received the author's coordinate in the payload whether or not anything on
    screen would ever draw it - and kept receiving it after the author paused,
    since the row still holds what it held. Removing the fields is the fix; a
    client cannot leak what it was never sent.

    `place_name` survives only while the story is a pin the viewer could see on
    the map anyway. It is a location under another name ("Blue Bottle, 8th
    Ave"), so it answers to the same rule the coordinate does.
    """

    place_name = serializers.SerializerMethodField()

    class Meta(StorySerializer.Meta):
        fields = [
            f for f in StorySerializer.Meta.fields
            if f not in ('latitude', 'longitude')
        ]

    def get_place_name(self, obj):
        # `is_placed_for`, not `can_show_on_map`: every row that reaches this
        # serializer came out of a queryset already filtered by `readable_q`,
        # so re-deriving the relationship here would be a pair of queries per
        # story to re-answer a question already answered in SQL.
        request = self.context.get('request')
        if request is None:
            return ''
        return obj.place_name if is_placed_for(request.user, obj) else ''


class StoryCreateSerializer(ScreenedTextMixin, serializers.ModelSerializer):
    """Note what is *not* here: `user`, and the final say over the position.

    Latitude and longitude are optional because a locationless story is a
    normal story, and the pair the client sends is a proposal rather than the
    stored value - CreateStoryView runs it through the author's location
    sharing mode, which may fuzz it or drop it entirely.

    `content` is optional too, since 0009 - a photo posted without commentary
    is the commonest story there is. What is *not* optional is having one or
    the other, which `validate` below is what enforces.
    """

    #: The caption, and the place label the author typed alongside it. Both
    #: are things a person wrote and both are published, so both are screened.
    screened_fields = ('content', 'place_name')

    class Meta:
        model = Story
        fields = ['content', 'image', 'image_url', 'latitude', 'longitude', 'place_name']

    def validate(self, attrs):
        """A story needs a caption or a picture. Neither is an empty post.

        Checked across the three fields rather than on any one of them: this is
        a rule about the pair, and `content` and `image` each have a perfectly
        good reason to be blank on their own. `image_url` counts because rows
        from before the R2 migration carry their picture there - see
        `Story.image_url` - and a client still posting that way is posting a
        picture.
        """
        has_text = bool((attrs.get('content') or '').strip())
        has_image = bool(attrs.get('image') or (attrs.get('image_url') or '').strip())
        if not has_text and not has_image:
            raise serializers.ValidationError(
                {'detail': 'A story needs a photo or some text.',
                 'code': 'story_empty'},
            )
        return attrs


class StoryReactionSerializer(serializers.Serializer):
    reaction = serializers.ChoiceField(choices=StoryReaction.ReactionType.choices)
