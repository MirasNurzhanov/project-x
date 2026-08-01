from datetime import date
from django.db.models import Q
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from friends.models import FriendRequest, HiRequest
from . import visibility
from .models import ProfileLink, User
from .presets import PRESET_MAP, ZONE_PRESETS
from interests.models import Interest
from interests.serializers import InterestSerializer
from regions.models import Region
from regions.serializers import RegionSummarySerializer


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'password2', 'date_of_birth')

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})

        dob = attrs.get('date_of_birth')
        if dob:
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 13:
                raise serializers.ValidationError({'date_of_birth': 'You must be at least 13 to register.'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password2')
        return User.objects.create_user(**validated_data)


class UserProfileSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    zone_audio_url = serializers.SerializerMethodField()
    links = serializers.SerializerMethodField()
    # The id is what a client writes back; the expansion saves it a lookup just
    # to show the name of the city it already chose.
    zone_region_detail = RegionSummarySerializer(source='zone_region', read_only=True)

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'bio', 'profile_picture',
            'date_of_birth', 'age_tier', 'interests',
            'latitude', 'longitude',
            'location_sharing', 'privacy_mode', 'date_joined', 'links',
            'is_private',
            'visibility_interests', 'visibility_stories', 'visibility_stats',
            'visibility_zone_audio', 'visibility_connections', 'visibility_presence',
            'discoverable_by_username', 'discoverable_by_interest',
            'zone_audio', 'zone_preset', 'zone_audio_url', 'zone_active',
            'zone_mode', 'zone_radius', 'zone_region', 'zone_region_detail',
            'push_token',
        )
        # `privacy_mode` is the legacy active/ghost/invisible view of
        # `location_sharing`, kept so app builds predating the split keep working.
        read_only_fields = ('id', 'email', 'age_tier', 'date_joined', 'privacy_mode')

    def get_links(self, obj):
        # Your own profile always shows your own links.
        return serialize_links(obj)

    def get_zone_audio_url(self, obj):
        if obj.zone_preset and obj.zone_preset in PRESET_MAP:
            return PRESET_MAP[obj.zone_preset]['url']
        if obj.zone_audio:
            return obj.zone_audio.url
        return None


def _pending_relationship(me, obj):
    """The finer-grained tier the profile screen wants: a pending Hi in either
    direction, on top of the settled tiers visibility.py knows about."""
    hi = HiRequest.objects.filter(
        Q(sender=me, receiver=obj) | Q(sender=obj, receiver=me)
    ).exclude(status=HiRequest.Status.ACCEPTED).first()
    if hi:
        return 'hi_sent' if hi.sender_id == me.pk else 'hi_received'
    return None


class PublicProfileSerializer(serializers.Serializer):
    """Someone else's profile, assembled field by field.

    Built as an explicit dict rather than a ModelSerializer so that a hidden
    field is genuinely absent from the payload — there is no declared field
    left behind holding a null for a modified client to notice.
    """

    def to_representation(self, obj):
        me = self.context['request'].user
        rel = visibility.relationship(me, obj)

        data = {
            'id': obj.id,
            'username': obj.username,
            'profile_picture': obj.profile_picture.url if obj.profile_picture else None,
            'is_private': obj.is_private,
            'relationship': _pending_relationship(me, obj) or rel,
        }

        presence = visibility.presence_for(me, obj, rel)
        if presence is not None:
            data['presence'] = presence

        # Minimal profile: username, avatar, and the action to ask for access.
        if visibility.minimal_only(me, obj, rel):
            return data

        data['bio'] = obj.bio
        data['date_joined'] = obj.date_joined

        if visibility.can_view(me, obj, 'interests', rel):
            data['interests'] = InterestSerializer(obj.interests.all(), many=True).data

        if visibility.can_view(me, obj, 'stats', rel):
            data['stats'] = _profile_stats(obj)

        if visibility.can_view(me, obj, 'connections', rel):
            data['connections'] = _connection_summaries(obj, viewer=me)

        if visibility.can_view(me, obj, 'stories', rel):
            data['stories'] = _live_story_summaries(obj)

        if visibility.can_view(me, obj, 'zone_audio', rel):
            data['zone_audio_url'] = _zone_audio_url(obj)

        # Absent, not empty: an empty list would still confirm the setting
        # exists and that this profile has links to hide.
        if visibility.can_view(me, obj, 'links', rel):
            data['links'] = serialize_links(obj)

        return data


def _profile_stats(obj):
    friend_ids = set()
    for s, r in FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED,
    ).filter(Q(sender=obj) | Q(receiver=obj)).values_list('sender_id', 'receiver_id'):
        friend_ids.add(r if s == obj.pk else s)

    connection_ids = set()
    for s, r in HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED,
    ).filter(Q(sender=obj) | Q(receiver=obj)).values_list('sender_id', 'receiver_id'):
        connection_ids.add(r if s == obj.pk else s)

    return {
        'friends': len(friend_ids),
        'connections': len(connection_ids - friend_ids),
    }


def _connection_summaries(obj, viewer):
    """`obj`'s connections and friends, minus anyone `viewer` is blocked with —
    a block must hold even when the pair is listed on a third party's profile."""
    ids = set()
    for model in (HiRequest, FriendRequest):
        for s, r in model.objects.filter(
            status=model.Status.ACCEPTED,
        ).filter(Q(sender=obj) | Q(receiver=obj)).values_list('sender_id', 'receiver_id'):
            ids.add(r if s == obj.pk else s)

    ids -= visibility.blocked_ids_for(viewer)
    ids.discard(obj.pk)

    return [
        {
            'id': u.id,
            'username': u.username,
            'profile_picture': u.profile_picture.url if u.profile_picture else None,
        }
        for u in User.objects.filter(pk__in=ids).only('id', 'username', 'profile_picture')
    ]


def _live_story_summaries(obj):
    """Unexpired stories. Coordinates are deliberately left out — a profile
    listing is not a location feed."""
    from django.utils import timezone
    from stories.models import Story

    return [
        {
            'id': s.id,
            'image': s.image.url if s.image else None,
            'content': s.content,
            'place_name': s.place_name,
            'created_at': s.created_at,
        }
        for s in Story.objects.filter(user=obj, expires_at__gt=timezone.now()).order_by('-created_at')
    ]


def _zone_audio_url(obj):
    if obj.zone_preset and obj.zone_preset in PRESET_MAP:
        return PRESET_MAP[obj.zone_preset]['url']
    if obj.zone_audio:
        return obj.zone_audio.url
    return None


class UserSearchSerializer(serializers.ModelSerializer):
    relationship_status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture', 'is_private', 'relationship_status')

    def to_representation(self, obj):
        data = super().to_representation(obj)
        me = self.context['request'].user
        rel = self.context.get('relationships', {}).get(obj.pk)
        if visibility.can_view(me, obj, 'interests', rel):
            data['interests'] = InterestSerializer(obj.interests.all(), many=True).data
        presence = visibility.presence_for(me, obj, rel)
        if presence is not None:
            data['presence'] = presence
        return data

    def get_relationship_status(self, obj):
        sent = self.context.get('sent_requests', {})
        received = self.context.get('received_requests', {})
        if obj.pk in sent:
            return 'friends' if sent[obj.pk] == 'accepted' else 'pending_sent' if sent[obj.pk] == 'pending' else 'none'
        if obj.pk in received:
            return 'friends' if received[obj.pk] == 'accepted' else 'pending_received' if received[obj.pk] == 'pending' else 'none'
        return 'none'


class ProfileLinkSerializer(serializers.ModelSerializer):
    """Read-only shape. Everything here is derived server-side from the
    canonical URL — the client renders `label` as plain text next to a local
    icon chosen by `platform`, and never parses `url` itself."""

    class Meta:
        model = ProfileLink
        fields = ('id', 'url', 'platform', 'handle', 'label', 'position')
        read_only_fields = fields


def serialize_links(user):
    return ProfileLinkSerializer(user.links.all(), many=True).data


class PrivacySettingsSerializer(serializers.ModelSerializer):
    """Everything on the Privacy & safety screen, read and written as one
    object so the client never has to guess which endpoint owns a setting."""

    class Meta:
        model = User
        fields = (
            'location_sharing',
            'is_private',
            'visibility_interests',
            'visibility_stories',
            'visibility_stats',
            'visibility_zone_audio',
            'visibility_connections',
            'visibility_links',
            'visibility_presence',
            'discoverable_by_username',
            'discoverable_by_interest',
        )


class ZoneUpdateSerializer(serializers.ModelSerializer):
    zone_audio_url = serializers.SerializerMethodField()
    # Written by id, read back in full, so the client that just set a region
    # can render its name without a second round trip.
    zone_region = serializers.PrimaryKeyRelatedField(
        queryset=Region.objects.all(), allow_null=True, required=False,
    )
    zone_region_detail = RegionSummarySerializer(source='zone_region', read_only=True)
    effective_zone_mode = serializers.CharField(read_only=True)
    # Declared explicitly to own the error message. Left to the model, DRF
    # derives the field from the MinValue/MaxValue validators and answers an
    # over-large radius with "ensure this value is less than or equal to
    # 5000" — which does not tell anyone that the way to cover more ground is
    # to switch modes. The model validators stay for the admin and direct saves.
    zone_radius = serializers.IntegerField(
        required=False,
        min_value=User.ZONE_RADIUS_MIN_M,
        max_value=User.ZONE_RADIUS_MAX_M,
        error_messages={
            'min_value': f'A zone cannot be smaller than {User.ZONE_RADIUS_MIN_M}m.',
            'max_value': (
                f'A circular zone tops out at {User.ZONE_RADIUS_MAX_M // 1000}km. '
                'Switch to a region to cover a whole city.'
            ),
        },
    )

    class Meta:
        model = User
        fields = (
            'zone_audio', 'zone_preset', 'zone_audio_url', 'zone_active',
            'zone_mode', 'zone_radius', 'zone_region', 'zone_region_detail',
            'effective_zone_mode',
        )

    def get_zone_audio_url(self, obj):
        if obj.zone_preset and obj.zone_preset in PRESET_MAP:
            return PRESET_MAP[obj.zone_preset]['url']
        if obj.zone_audio:
            return obj.zone_audio.url
        return None

    def validate_zone_preset(self, value):
        if value and value not in PRESET_MAP:
            raise serializers.ValidationError(f"'{value}' is not a valid preset id.")
        return value

    def validate(self, attrs):
        # Region mode needs a region: either arriving in this request or
        # already stored. Without one the zone would have no shape at all.
        mode = attrs.get('zone_mode') or (self.instance and self.instance.zone_mode)
        if mode == User.ZoneMode.REGION:
            region = attrs['zone_region'] if 'zone_region' in attrs else (
                self.instance and self.instance.zone_region
            )
            if not region:
                raise serializers.ValidationError(
                    {'zone_region': 'Pick a city or region before switching to region mode.'}
                )

        has_audio = attrs.get('zone_audio') or (
            'zone_audio' not in attrs and self.instance and self.instance.zone_audio
        )
        has_preset = attrs.get('zone_preset') or (
            'zone_preset' not in attrs and self.instance and self.instance.zone_preset
        )
        if attrs.get('zone_audio') and attrs.get('zone_preset'):
            raise serializers.ValidationError('Set either zone_audio or zone_preset, not both.')
        # Uploading a new file clears any existing preset
        if attrs.get('zone_audio') and has_preset:
            attrs['zone_preset'] = ''
        # Selecting a preset clears any existing uploaded file
        if attrs.get('zone_preset') and has_audio:
            attrs['zone_audio'] = None
        return attrs


class EditProfileSerializer(serializers.ModelSerializer):
    interests = serializers.PrimaryKeyRelatedField(
        queryset=Interest.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = User
        fields = ('bio', 'profile_picture', 'date_of_birth', 'interests', 'push_token')

    def update(self, instance, validated_data):
        interests = validated_data.pop('interests', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if interests is not None:
            instance.interests.set(interests)

        return instance
