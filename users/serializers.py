from datetime import date
from django.db.models import Q
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from friends.models import FriendRequest, HiRequest
from . import visibility, zones
from .models import ProfileLink, User
from interests.models import Interest
from interests.serializers import InterestSerializer
from regions.models import Region
from regions.serializers import RegionSummarySerializer


#: The floor Aura's terms set. Lives up here rather than inline in one
#: serializer because there are now two ways to first state a date of birth —
#: registering, and filling it in after a Google/Apple sign-up, which arrives as
#: an ordinary profile PATCH. A gate that only guards one of them is not a gate.
MIN_AGE = 13


def age_on(dob, today=None):
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_min_age(dob):
    if dob and age_on(dob) < MIN_AGE:
        raise serializers.ValidationError(f'You must be at least {MIN_AGE} to use Aura.')
    return dob


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'email', 'password', 'password2', 'date_of_birth')

    def validate_date_of_birth(self, value):
        return validate_min_age(value)

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
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
    connected_providers = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'bio', 'profile_picture',
            'date_of_birth', 'age_tier', 'nationality', 'profession', 'interests',
            'latitude', 'longitude',
            'location_sharing', 'privacy_mode', 'date_joined', 'links',
            'is_private',
            'visibility_interests', 'visibility_stories', 'visibility_stats',
            'visibility_zone_audio', 'visibility_connections', 'visibility_presence',
            'discoverable_by_username', 'discoverable_by_interest',
            'zone_audio', 'zone_audio_url', 'zone_active',
            'zone_type', 'zone_radius', 'zone_region', 'zone_region_detail',
            'push_token', 'connected_providers',
        )
        # `privacy_mode` is the legacy active/ghost/invisible view of
        # `location_sharing`, kept so app builds predating the split keep working.
        read_only_fields = ('id', 'email', 'age_tier', 'date_joined', 'privacy_mode')

    def get_links(self, obj):
        # Your own profile always shows your own links.
        return serialize_links(obj)

    def get_zone_audio_url(self, obj):
        # Shared with the zone and public-profile serializers, so an Apple track
        # cannot be the answer on one endpoint and invisible on another.
        return _zone_audio_url(obj)

    def get_connected_providers(self, obj):
        # What Settings' "Connected accounts" row needs to show Connect vs
        # Connected — a list rather than two booleans so a third provider
        # never needs a new field, just a new entry.
        return list(obj.social_accounts.values_list('provider', flat=True))


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
        data['date_of_birth'] = obj.date_of_birth
        data['nationality'] = obj.nationality
        data['profession'] = obj.profession
        data['clubs'] = _club_memberships(obj, viewer=me)

        if visibility.can_view(me, obj, 'interests', rel):
            data['interests'] = InterestSerializer(obj.interests.all(), many=True).data

        if visibility.can_view(me, obj, 'stats', rel):
            data['stats'] = _profile_stats(obj)

        if visibility.can_view(me, obj, 'connections', rel):
            data['connections'] = _connection_summaries(obj, viewer=me)

        if visibility.can_view(me, obj, 'stories', rel):
            data['stories'] = _live_story_summaries(obj, viewer=me)

        # A removed music zone is absent here too, not just on the map. A
        # profile that still offered a play button for a zone its owner had
        # switched off would be a second, quieter way to hear someone who has
        # gone quiet.
        if visibility.can_view(me, obj, 'zone_audio', rel) and obj.music_zone_active:
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


def _live_story_summaries(obj, viewer=None):
    """Unexpired stories. Coordinates are deliberately left out — a profile
    listing is not a location feed.

    Nor is the place name, once its author is off the map: "Blue Bottle, 8th
    Ave" says where somebody is as plainly as a pair of decimals does, and this
    is the third surface it could have escaped through after the map feed and
    the saved list. The stories themselves stay - pausing takes you off the
    map, it does not withdraw you from your friends.
    """
    from django.utils import timezone
    from stories.models import Story

    # `is_sharing`, matching stories.visibility.is_placed_for — a place name on
    # a story says where that story was posted, not where its author is now.
    placed = obj.pk == getattr(viewer, 'pk', None) or obj.is_sharing()
    return [
        {
            'id': s.id,
            # `image_url` first: a photo uploaded straight to Cloudinary, before
            # the R2 migration retired that path, never touches the `image`
            # field, and reading only that one renders
            # every such story blank here.
            'image': s.image_url or (s.image.url if s.image else None),
            'content': s.content,
            'place_name': s.place_name if placed else '',
            'created_at': s.created_at,
        }
        for s in Story.objects.filter(user=obj, expires_at__gt=timezone.now()).order_by('-created_at')
    ]


def _club_memberships(obj, viewer=None):
    """The clubs on somebody's profile, minus the ones the viewer may not see.

    A profile is a list of clubs like any other, and without this it is the
    easy way round the location rule: read a stranger's profile, learn the name
    and id of a club scoped to a district on the other side of the country.

    The viewer's own memberships come through regardless — that is the same
    membership rule the club page applies, and a member seeing their own club
    named on a friend's profile is not a leak.
    """
    from clubs.models import ClubMember
    from clubs import visibility as club_visibility

    qs = ClubMember.objects.filter(user=obj).select_related('club')
    if viewer is not None:
        place = club_visibility.place_for(viewer)
        qs = [
            m for m in qs
            if club_visibility.can_access(m.club, viewer, place)
        ]
    return [
        {
            'id': m.club_id,
            'name': m.club.name,
            'emblem': m.club.emblem.url if m.club.emblem else None,
            'role': m.role,
        }
        for m in qs
    ]


#: The Apple track slot, emptied. Five fields move together or the zone ends up
#: showing one song's artwork over another's audio.
_CLEARED_APPLE_TRACK = {
    'zone_apple_id': '',
    'zone_apple_title': '',
    'zone_apple_artist': '',
    'zone_apple_artwork': '',
    'zone_apple_preview_url': '',
}

#: The loop, back to the whole track. Applied whenever the track changes:
#: "seconds 40 to 55" was chosen while listening to one song and is arbitrary
#: in the next, and silently carrying it over would leave someone's zone
#: looping a window they never picked — or, on a shorter track, past its end.
_CLEARED_LOOP = {
    'zone_track_start_ms': 0,
    'zone_track_end_ms': None,
}


def _zone_audio_url(obj):
    # One zone plays one thing, and this is the order that decides which. An
    # Apple track wins because picking one clears the upload — the order only
    # matters for rows written before that rule existed.
    if obj.zone_apple_preview_url:
        return obj.zone_apple_preview_url
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
    object so the client never has to guess which endpoint owns a setting.

    `location_sharing` is the writable field that decides whether this user is
    on the map at all, and this is the endpoint that writes it. That is a
    settings PATCH doing something App Review cares about, so the surrounding
    conditions carry the weight: the field defaults to PAUSED, the client puts
    a consent screen in front of every move off PAUSED, and moving *to* PAUSED
    is always allowed and takes effect at once. See PrivacySettingsView, which
    is where the side effects of the transition live.
    """

    class Meta:
        model = User
        fields = (
            'location_sharing',
            # Read-only, and read *once*: the client keeps it in memory for the
            # life of the process and never writes it to disk. Persisting it
            # would rebuild the thing it exists to prevent — a token that
            # survives a restart is a preference that says you were visible.
            'sharing_session',
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
        read_only_fields = ('sharing_session',)


class NotificationSettingsSerializer(serializers.ModelSerializer):
    """The Notifications screen, read and written as one object.

    Same shape and the same reasoning as PrivacySettingsSerializer: one
    endpoint owns the whole screen, so a client never has to know which switch
    lives where. `music_zone_alerts` is included even though it also appears on
    the zone endpoint — it is a notification switch, and a user looking for
    "stop telling me about music" looks here first.
    """

    class Meta:
        model = User
        fields = (
            'notify_messages',
            'notify_club_messages',
            'notify_club_events',
            'notify_social',
            'notify_story_activity',
            'music_zone_alerts',
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

    # Same treatment as `zone_radius` above, with its own copy: the ceiling on
    # a music zone is not the ceiling on a zone, and telling someone to "switch
    # to a region" would be nonsense advice about a circle that has no region
    # mode.
    music_zone_radius = serializers.IntegerField(
        required=False,
        min_value=User.ZONE_RADIUS_MIN_M,
        max_value=User.ZONE_RADIUS_MAX_M,
        error_messages={
            'min_value': f'A music zone cannot be smaller than {User.ZONE_RADIUS_MIN_M}m.',
            'max_value': f'A music zone tops out at {User.ZONE_RADIUS_MAX_M // 1000}km.',
        },
    )

    class Meta:
        model = User
        fields = (
            'zone_audio', 'zone_audio_url', 'zone_active',
            'zone_type', 'zone_radius', 'zone_region', 'zone_region_detail',
            'effective_zone_mode',
            # One switch and one radius. `music_radius_m` used to sit here
            # resolving "separate or inherited" for the client; there is no
            # question left to resolve.
            'music_zone_active', 'music_zone_radius', 'music_zone_alerts',
            # Written through ZoneAppleMusicView, which resolves an id against
            # Apple rather than trusting a phone with a URL the map will play.
            'zone_apple_id', 'zone_apple_title', 'zone_apple_artist',
            'zone_apple_artwork', 'zone_apple_preview_url',
            'zone_track_start_ms', 'zone_track_end_ms',
        )
        read_only_fields = (
            'zone_apple_id', 'zone_apple_title', 'zone_apple_artist',
            'zone_apple_artwork', 'zone_apple_preview_url',
        )

    def get_zone_audio_url(self, obj):
        return _zone_audio_url(obj)

    def validate(self, attrs):
        # Two independent rulesets, kept apart because they have nothing to say
        # to each other: what the zone *is*, and which slice of its track loops.
        return self._validate_loop(self._validate_zone(attrs))

    def _validate_zone(self, attrs):
        # A polygon type needs a boundary: either arriving in this request or
        # already stored. Without one the zone would have no shape at all, and
        # the containment service raises rather than inventing a circle — so
        # this has to be refused at the door instead of stored and discovered
        # later by whichever feature asked first.
        zone_type = attrs.get('zone_type') or (self.instance and self.instance.zone_type)
        region = attrs['zone_region'] if 'zone_region' in attrs else (
            self.instance and self.instance.zone_region
        )

        if zone_type in User.POLYGON_ZONE_TYPES:
            if not region:
                raise serializers.ValidationError({
                    'zone_region': f'Pick a place before switching to a {zone_type} zone.',
                    'code': zones.BOUNDARY_NOT_AVAILABLE,
                })
            # The type the client asked for has to match the place the server
            # resolved. Otherwise "district" is just a word the phone sent: a
            # user could label a whole-city boundary a district zone, and every
            # containment check would honour the city polygon while the API and
            # the UI both said district.
            #
            # `other` is refused for both, which is stricter than it may look.
            # It is not "a coarse city" — regions.nominatim classifies a place
            # `other` precisely when it is *neither* of the two sizes the
            # product offers, and its own docstring names provinces and
            # countries as the cases. Accepting one as a city zone would let
            # somebody cover an entire province, and the containment service
            # would honour it to the letter.
            #
            # A place that genuinely is a city but resolved as `other` is
            # refused rather than approximated. That is the specified
            # degradation: an explicit error the user can see beats a zone
            # silently three orders of magnitude larger than the one they asked
            # for. The resolver can promote an `other` to a real size when the
            # address hierarchy corroborates it, which is where that case is
            # meant to be fixed.
            if region.kind != zone_type:
                raise serializers.ValidationError({'zone_region': (
                    f'{region.name} is a {region.get_kind_display().lower()}, '
                    f'which cannot be used as a {zone_type} zone.'
                )})

        # A circle is deliberately *not* checked for a centre here, though the
        # equivalent rule for a polygon is enforced above. The asymmetry is
        # real: a polygon zone's boundary is chosen and permanent, so a missing
        # one is a broken zone, whereas a circle is centred on the user's live
        # position and a missing one only means GPS has not reported yet.
        # Refusing the save would block someone dialling a radius on a cold
        # start, and it would fix itself a second later. `zone_contains` raises
        # for that state instead, which is the honest place for a condition
        # that is temporary rather than invalid.

        # Your zone covers where you are. Enforced here rather than left to the
        # UI, because "only the place you are standing in" is the rule itself,
        # not a convenience: the endpoint takes a region id, and without this
        # any id would be accepted — someone in one city could blanket another.
        # Only checked when a region is newly supplied, so moving out of your
        # own city later does not start rejecting unrelated zone edits.
        incoming = attrs.get('zone_region')
        if incoming is not None:
            user = self.instance
            lat, lon = getattr(user, 'latitude', None), getattr(user, 'longitude', None)
            if lat is None or lon is None:
                raise serializers.ValidationError(
                    {'zone_region': 'Aura needs your location before it can set a zone here.'}
                )
            # Tested against the position we *store*, which on Approximate
            # sharing is a ~500m grid-cell centre rather than where the user is
            # standing. Checking that strictly told people in Бостандық that
            # they were not in Бостандық, because the cell centre had landed
            # over the border in Алмалы. The tolerance is the error we
            # introduced ourselves — zero for anyone sharing precisely — so the
            # rule this exists to enforce, that you cannot blanket a city you
            # are not in, is unchanged for them and only softened by a few
            # hundred metres at the border for everyone else.
            if not incoming.could_contain(
                float(lat), float(lon), visibility.position_tolerance_m(user),
            ):
                raise serializers.ValidationError(
                    {'zone_region': (
                        f'You can only cover a place you are in, and the location Aura '
                        f'has for you is not inside {incoming.name}. If you have just '
                        f'moved, open the map for a moment so it can catch up.'
                    )}
                )

        # An upload clears an Apple track, which is the other way to fill the
        # same slot. Without this a user who picked a song and then uploaded a
        # file would keep hearing the song: `_zone_audio_url` reads the Apple
        # field first.
        if attrs.get('zone_audio'):
            attrs.update(_CLEARED_APPLE_TRACK)

        # A new track means a new loop. Cleared on any change of source —
        # including clearing one, so a silent zone does not keep a window.
        if 'zone_audio' in attrs and 'zone_track_start_ms' not in attrs:
            attrs.update(_CLEARED_LOOP)

        return attrs

    def _validate_loop(self, attrs):
        """The loop has to be a real window: ordered, and long enough to be a
        loop rather than a stutter.

        The track's true length is not known here — it lives in the audio file,
        which this server never decodes — so an end past the end of the track
        cannot be rejected. The player clamps to the duration it actually
        loaded, which is the only place that number exists.
        """
        start = attrs.get('zone_track_start_ms')
        end = attrs.get('zone_track_end_ms')
        if start is None and self.instance:
            start = self.instance.zone_track_start_ms
        if end is None and 'zone_track_end_ms' not in attrs and self.instance:
            end = self.instance.zone_track_end_ms

        if end is not None:
            if end <= (start or 0):
                raise serializers.ValidationError(
                    {'zone_track_end_ms': 'The loop has to end after it starts.'}
                )
            if end - (start or 0) < User.MIN_LOOP_MS:
                raise serializers.ValidationError({
                    'zone_track_end_ms':
                        f'A loop shorter than {User.MIN_LOOP_MS // 1000} seconds is a stutter, '
                        'not a track.',
                })
        return attrs


class EditProfileSerializer(serializers.ModelSerializer):
    interests = serializers.PrimaryKeyRelatedField(
        queryset=Interest.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = User
        fields = (
            'username', 'bio', 'profile_picture', 'date_of_birth',
            'nationality', 'profession', 'interests', 'push_token',
        )

    # This is the route a Google/Apple sign-up takes to state its date of birth
    # for the first time, so the age floor has to hold here and not only at
    # registration — otherwise the social buttons are a way around it.
    def validate_date_of_birth(self, value):
        return validate_min_age(value)

    def validate_username(self, value):
        # The model's own uniqueness (via ModelSerializer's auto UniqueValidator)
        # is case-sensitive; everywhere a username is actually matched —
        # socialauth's _unique_username, sign-in by handle — it is not. Without
        # this, renaming to a handle that differs from someone else's only by
        # case would pass here and then collide there: two accounts nobody
        # could tell apart by name.
        taken = User.objects.filter(username__iexact=value).exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError('That username is already taken.')
        return value

    def update(self, instance, validated_data):
        interests = validated_data.pop('interests', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if interests is not None:
            instance.interests.set(interests)

        return instance
