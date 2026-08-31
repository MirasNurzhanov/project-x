from datetime import date
from django.db.models import Q
from moderation.drf import ScreenedTextMixin
from rest_framework import serializers
from rest_framework.exceptions import APIException
from django.contrib.auth.password_validation import validate_password
from friends.models import FriendRequest, HiRequest
from . import terms, visibility, zones
from .models import ProfileLink, User
from interests.models import Interest
from interests.serializers import InterestSerializer
from regions.models import Region
from regions.serializers import RegionSummarySerializer


#: The floor Approx's terms set. Lives up here rather than inline in one
#: serializer because there are now two ways to first state a date of birth —
#: registering, and filling it in after a Google/Apple sign-up, which arrives as
#: an ordinary profile PATCH. A gate that only guards one of them is not a gate.
MIN_AGE = 13


def age_on(dob, today=None):
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_min_age(dob):
    if dob and age_on(dob) < MIN_AGE:
        raise serializers.ValidationError(f'You must be at least {MIN_AGE} to use Approx.')
    return dob


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True)
    #: Agreement to the Terms of Use, Community Guidelines and Privacy Policy.
    #: Required, and required to be *true* — a registration that does not carry
    #: it is refused rather than created-and-gated, because there is no reason
    #: to make an account for somebody who has not agreed to the rules it is
    #: held to. `terms_version` is not accepted from the client: the server
    #: decides which version was in force, so a client cannot claim agreement
    #: to a document it invented.
    accept_terms = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = (
            'username', 'email', 'password', 'password2', 'date_of_birth',
            'accept_terms',
        )

    def validate_date_of_birth(self, value):
        return validate_min_age(value)

    def validate_accept_terms(self, value):
        if not value:
            raise serializers.ValidationError(
                'You must agree to the Terms of Use and Community Guidelines.'
            )
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password2')
        validated_data.pop('accept_terms')
        user = User.objects.create_user(**validated_data)
        # Stamped after creation rather than passed into create_user, so the
        # version comes from `users.terms` on the server in every path.
        terms.record_acceptance(user)
        return user


class UserProfileSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    zone_audio_url = serializers.SerializerMethodField()
    links = serializers.SerializerMethodField()
    # The id is what a client writes back; the expansion saves it a lookup just
    # to show the name of the city it already chose.
    zone_region_detail = RegionSummarySerializer(source='zone_region', read_only=True)
    connected_providers = serializers.SerializerMethodField()
    #: The answer the app's terms gate reads. A *derived* boolean rather than
    #: the raw columns, so the "is this version still current" rule lives on the
    #: server and an old build cannot get it wrong by comparing strings itself.
    needs_terms = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'bio', 'profile_picture',
            'date_of_birth', 'age_tier', 'nationality', 'profession', 'interests',
            'latitude', 'longitude',
            'ghost_mode',
            # Both derived, read-only, and kept for builds predating Ghost
            # Mode: `privacy_mode` is the active/ghost/invisible spelling and
            # `location_sharing` the live/approximate/paused one.
            'location_sharing', 'privacy_mode', 'date_joined', 'links',
            'is_private',
            'visibility_interests', 'visibility_stories', 'visibility_stats',
            'visibility_zone_audio', 'visibility_connections', 'visibility_presence',
            'discoverable_by_username', 'discoverable_by_interest',
            'zone_audio', 'zone_audio_url', 'zone_active',
            'zone_type', 'zone_radius', 'zone_region', 'zone_region_detail',
            'zone_follows_location', 'zone_follow_stale',
            'push_token', 'connected_providers',
            'terms_version', 'terms_accepted_at', 'needs_terms',
        )
        # `privacy_mode` and `location_sharing` are the legacy spellings of
        # `ghost_mode`, kept readable so app builds predating it keep working.
        #
        # `ghost_mode` is readable here and writable only through
        # PrivacySettingsSerializer. One writable route is what stops a user who
        # has turned themselves off being turned back on by a side effect of
        # some unrelated profile PATCH — see
        # `GhostIsOnlyChangedDeliberatelyTests`, which asserts this statically.
        read_only_fields = (
            'id', 'email', 'age_tier', 'date_joined', 'privacy_mode',
            'ghost_mode',
            # Server-set: see the note on the field in ZoneUpdateSerializer.
            'zone_follow_stale',
            # Acceptance is recorded by the server on the paths that actually
            # showed somebody the document. A PATCH must never be able to claim
            # it — that would make the gate bypassable from the client.
            'terms_version', 'terms_accepted_at',
        )

    def get_needs_terms(self, obj):
        return not terms.has_accepted(obj)

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

    `ghost_mode` is the writable field that decides whether this user is on the
    map at all, and this is the endpoint that writes it. It replaced a
    three-way `location_sharing`; the precision half of that value is gone
    entirely, because friends see a precise position and everybody else a
    ~500m cell and neither is the sharer's to pick. Turning Ghost *on* is
    always allowed and takes effect at once.
    """

    class Meta:
        model = User
        fields = (
            'ghost_mode',
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
            'notify_swift_activity',
            'music_zone_alerts',
        )


class OutsideRegion(APIException):
    """400, with a body the app can act on rather than only display.

    Deliberately **not** `serializers.ValidationError`. DRF funnels any
    ValidationError raised inside `validate()` through `as_serializer_error`,
    which coerces every leaf into an `ErrorDetail` string and wraps it in a
    list — so `you_are_in: {'id': 4, ...}` arrives as
    `you_are_in: ["{'id': ErrorDetail('4')}"]` and the id the client wanted to
    act on is gone. A plain `APIException` skips that machinery and the dict
    arrives the shape it was written in.

    This is the same trap, and the same fix, as
    `moderation.drf.ObjectionableContent`.
    """

    status_code = 400
    default_code = 'outside_region'

    def __init__(self, message, suggestion):
        super().__init__(message)
        # Assigned *after* `super().__init__`, deliberately, and not passed to
        # it. `APIException` runs its detail through `_get_error_details`, which
        # recurses into dicts and lists and wraps every leaf in an
        # `ErrorDetail` — a `str` subclass. That turns `id: 4` into `"4"` and,
        # worse, `you_are_in: None` into the *string* `"None"`, which a client
        # checking for null would happily act on. DRF's exception handler uses
        # `exc.detail` verbatim when it is a dict, so setting it here is what
        # gets the body onto the wire in the shape it was written.
        self.detail = {
            # Under the field name too, so a client that only knows how to show
            # `zone_region[0]` keeps working unchanged.
            'zone_region': [message],
            'detail': message,
            'code': 'outside_region',
            'you_are_in': suggestion,
        }


class ZoneUpdateSerializer(serializers.ModelSerializer):
    zone_audio_url = serializers.SerializerMethodField()
    # Written by id, read back in full, so the client that just set a region
    # can render its name without a second round trip.
    zone_region = serializers.PrimaryKeyRelatedField(
        queryset=Region.objects.all(), allow_null=True, required=False,
    )
    zone_region_detail = RegionSummarySerializer(source='zone_region', read_only=True)
    effective_zone_mode = serializers.CharField(read_only=True)
    #: Where the phone says it is, *for this check only*.
    #:
    #: The rule below — you can only cover a place you are in — used to be
    #: tested against `user.latitude`, the position the server had stored. That
    #: position is written by a different endpoint on a different schedule, so
    #: the two disagree constantly: open the app straight into Settings → Zone
    #: Size, or walk a few streets, and the zone screen is looking at a live
    #: GPS fix while the validator is looking at wherever the map last ticked.
    #: The result is being told you are not standing where you are standing,
    #: with no reliable way to fix it — which is the bug this exists to remove
    #: rather than paper over.
    #:
    #: **Never stored.** These fields are popped in `validate` and never reach
    #: the instance, so setting a zone writes no position and a user with
    #: sharing paused can configure one without broadcasting anything. That is
    #: the same arrangement `proximity.views.NearbyQueryView` uses for private
    #: browsing, and the same one `/api/regions/for-point/` already relies on —
    #: the phone sends the raw fix there too.
    #:
    #: No weaker than what it replaces. The alternative to trusting this is
    #: trusting the identical coordinate the same phone can PATCH to
    #: `/api/proximity/location/` a moment earlier, which is what it had to do
    #: before. The check is a guard against covering a city you are nowhere
    #: near, not a proof of presence, and it was never the latter.
    latitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, write_only=True, required=False,
        allow_null=True,
    )
    longitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, write_only=True, required=False,
        allow_null=True,
    )
    #: Metres of GPS error the phone reports for that fix, widening the check by
    #: exactly the amount the fix is known to be wrong by. Clamped: an
    #: unbounded value would let a client claim a kilometre of slack and cover
    #: the next district over.
    accuracy = serializers.FloatField(
        write_only=True, required=False, allow_null=True, min_value=0,
    )

    #: Set by `validate`, read by `ZoneView.patch`. Defaults to None so a
    #: serializer used without validation reads as "no anchor" rather than
    #: raising — the same shape `ScreenedTextMixin.moderation_verdict` uses.
    validation_anchor = None

    #: Read-only: nothing but the server may say a zone is behind. It is set on
    #: the location path when a following zone had nowhere to move to, and
    #: cleared the moment one resolves — a client that could write it could
    #: silence the only signal the zone screen has that the boundary on it is
    #: no longer the one the user is standing in.
    zone_follow_stale = serializers.BooleanField(read_only=True)
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
            'zone_follows_location', 'zone_follow_stale',
            'effective_zone_mode',
            # Write-only, validation-only, never stored — see the note above.
            'latitude', 'longitude', 'accuracy',
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
        attrs = self._validate_loop(self._validate_zone(attrs))
        # Popped *after* validation and before `save()` ever sees them. These
        # three are the only fields on this serializer that are not columns —
        # leaving them in would try to write a position through a zone edit,
        # which is precisely what this endpoint must not do.
        for field in ('latitude', 'longitude', 'accuracy'):
            attrs.pop(field, None)
        return attrs

    #: Ceiling on the slack a client's own accuracy figure may buy it. A fix
    #: really can be a kilometre out indoors, and honouring that verbatim would
    #: let anyone cover the next district by claiming a bad one. 750m is
    #: comfortably past the worst genuinely useful urban fix and comfortably
    #: short of a district.
    MAX_ANCHOR_TOLERANCE_M = 750.0

    def _anchor(self, attrs):
        """`(lat, lon, tolerance_m)` to test containment against, or None.

        Two sources, in this order, and the order is the whole fix:

          1. **The position sent with this request.** The phone has a live fix
             on screen — it drew the preview with it — so the check runs
             against what the user is actually looking at. Tolerance is that
             fix's own reported accuracy, which is the error that genuinely
             exists, clamped.

          2. **The position the server has stored**, for a client too old to
             send one. Its tolerance is zero: what we store is the precise fix,
             for everybody. It used to carry the error *we* introduced by
             fuzzing an Approximate sharer before storage, which no longer
             happens — fuzzing moved to the read, where it applies to other
             viewers and not to the owner checking their own zone.

        The stored position being second is the point. It is written by a
        different endpoint on a different schedule, and every report of "it says
        I am not where I am" traces back to reading it as though it were
        current. It stays as a fallback rather than being deleted because an
        installed build that does not send an anchor must keep working.
        """
        lat = attrs.get('latitude')
        lon = attrs.get('longitude')
        if lat is not None and lon is not None:
            accuracy = attrs.get('accuracy') or 0.0
            return (
                float(lat), float(lon),
                min(max(float(accuracy), 0.0), self.MAX_ANCHOR_TOLERANCE_M),
            )

        user = self.instance
        stored_lat = getattr(user, 'latitude', None)
        stored_lon = getattr(user, 'longitude', None)
        if stored_lat is None or stored_lon is None:
            return None
        return (float(stored_lat), float(stored_lon), 0.0)

    def _place_you_are_in(self, lat, lon, rejected):
        """The region of the same size that *does* contain this position.

        Returned with the refusal so the app can offer it. A rejection on its
        own is a dead end — the user is told the place they chose is wrong and
        left to guess which one is right, which for somebody who does not know
        their district's official name is not a guess they can make. Naming it
        turns the error into a one-tap correction.

        Cache-only: this runs inside a validation failure, and reaching
        Nominatim to explain a 400 would make the error slower than the success.
        Returns None when nothing is cached, which the client renders as the
        plain message.
        """
        from regions import places

        try:
            hierarchy = places.hierarchy_for_point(lat, lon, allow_upstream=False)
        except Exception:  # noqa: BLE001 - explaining an error must not raise one
            return None

        for area in hierarchy.areas or ():
            if area.kind == rejected.kind and area.pk != rejected.pk:
                return {'id': area.pk, 'name': area.name, 'kind': area.kind}
        return None

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
        #
        # Only when the region is actually **changing**. This used to fire
        # whenever `zone_region` appeared in the payload, which is not the same
        # thing and is the difference between a rule and a trap: re-sending the
        # region you already have — which the app does the moment you tap the
        # Place tab — was refused as soon as you left it. Refusing that does not
        # un-set the zone, because it was already stored; all it does is block
        # every edit that mentions it, including the one you came to make. The
        # symptom is being unable to switch to the Place tab at all, and so
        # being unable to reach the picker to choose the district you have
        # moved into.
        #
        # Nothing is loosened by this. Leaving a place you cover was never
        # supposed to be a refusal — the header comment above has said "moving
        # out of your own city later does not start rejecting unrelated zone
        # edits" since this was written, and this is the line that finally
        # makes that true. Pointing the zone at a *new* place is still checked
        # exactly as before.
        anchor = self._anchor(attrs)
        #: Read by `ZoneView.patch`. The position this request was validated
        #: against is also the right one to re-point a following zone from —
        #: re-resolving from the stored copy there would reintroduce the exact
        #: staleness the anchor exists to sidestep.
        self.validation_anchor = anchor
        incoming = attrs.get('zone_region')
        unchanged = (
            incoming is not None
            and self.instance is not None
            and incoming.pk == self.instance.zone_region_id
        )
        if incoming is not None and not unchanged:
            if anchor is None:
                raise serializers.ValidationError(
                    {'zone_region': 'Approx needs your location before it can set a zone here.'}
                )
            lat, lon, tolerance = anchor
            if not incoming.could_contain(lat, lon, tolerance):
                # Machine-readable, so the app can offer the place the user
                # *is* in rather than dead-ending on an alert. See
                # `_place_you_are_in` for why this is worth the query.
                raise OutsideRegion(
                    f'You can only cover a place you are in, and you do not seem '
                    f'to be inside {incoming.name}.',
                    self._place_you_are_in(lat, lon, incoming),
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


class EditProfileSerializer(ScreenedTextMixin, serializers.ModelSerializer):
    interests = serializers.PrimaryKeyRelatedField(
        queryset=Interest.objects.all(),
        many=True,
        required=False,
    )

    #: A profile is public text and takes the same screening a caption does.
    #: There is no quarantine state for a profile, so a flagged bio saves
    #: normally and lands in the queue via the report `validate` files below -
    #: hiding somebody's account on a maybe would be a heavier answer than the
    #: signal deserves, and the account itself is still reportable and blockable
    #: in the meantime.
    screened_fields = ('bio', 'username', 'profession')

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

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # A flagged bio is saved, but somebody should look at it. Filed against
        # the account rather than a piece of content because that is what a bio
        # is - there is no separate row to quarantine.
        verdict = self.moderation_verdict
        if verdict.flagged and self.instance is not None:
            from moderation import services as moderation_services
            from moderation.models import ContentReport

            moderation_services.file_report(
                self.instance, ContentReport.Target.USER, self.instance.pk,
                reason=verdict.reason,
                note=f'Automated filter on profile text: {", ".join(verdict.matched)}',
                source=ContentReport.Source.AUTOMATED,
            )
        return attrs

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
