from django.db.models import Count, IntegerField, OuterRef, Subquery
from rest_framework import serializers

from regions import places
from users.models import User
from .models import Club, ClubEvent, ClubInvitation, ClubMember, ClubMessage

#: Where `with_card_data` parks the two facts `ClubSerializer` would otherwise
#: fetch one club at a time. Named rather than inlined so the annotation and the
#: reader that falls back when it is absent cannot drift apart.
MEMBER_COUNT_ATTR = 'member_count_annotated'
MY_ROLE_ATTR = 'my_role_annotated'


def with_card_data(queryset, user):
    """Fetch a club list's member counts and the viewer's own role in one query.

    Without this, every club on the list costs three: one for the creator, one
    to count members, one to look up the viewer's membership. A thirty-five club
    discovery list was a hundred and seven queries, and the club screens poll it
    every fifteen seconds.

    Correlated subqueries rather than `annotate(Count('members'))` on purpose.
    Both call sites already filter on `members` — discovery excludes the
    viewer's own clubs, the joined list selects them — and a plain aggregate
    would reuse that join and count only the rows the filter left behind. A
    subquery brings its own join and cannot be narrowed by the outer one.
    """
    memberships = ClubMember.objects.filter(club=OuterRef('pk'))
    return queryset.select_related('creator').annotate(
        **{
            MEMBER_COUNT_ATTR: Subquery(
                memberships.order_by().values('club').annotate(
                    n=Count('pk'),
                ).values('n'),
                output_field=IntegerField(),
            ),
            # May legitimately be NULL — the viewer is not a member — which is
            # exactly what the un-annotated path returns too.
            MY_ROLE_ATTR: Subquery(
                memberships.filter(user=user).values('role')[:1],
            ),
        }
    )


def _tier(region_id, name):
    """One tier of a club's scope, or None when it was never recorded.

    The id may be null where the name is not: regions are a shared cache and a
    row can be evicted out from under a club that named it. Both go out, so a
    client can key on the id and still have something to print.
    """
    if not region_id and not name:
        return None
    return {'id': region_id, 'name': name}


class PlaceSerializer(serializers.Serializer):
    """Where the requesting user is, as the club screens need it.

    Doubles as the answer to "which visibility levels may I choose", which is
    not a separate question: a level is offerable exactly when we hold a
    boundary for that tier. See clubs.visibility.available_levels.
    """

    def to_representation(self, place):
        from . import visibility

        if place is None or not getattr(place, 'resolved', False):
            payload = {
                'resolved': False,
                'city': None,
                'region': None,
                'district': None,
                'country_code': '',
                'available_levels': [],
            }
            # The reason, and the sentence that goes with it. Written on the
            # server so the wording cannot drift between the club list and the
            # create screen, which show it in different places.
            payload.update(
                place.as_payload() if hasattr(place, 'as_payload')
                else {'location_status': 'no_location', 'detail': ''}
            )
            return payload

        payload = {
            'resolved': True,
            'location_status': 'ok',
            'detail': '',
            'country_code': place.country_code,
            'available_levels': visibility.available_levels(place),
            'updated_at': place.resolved_at,
        }
        for tier in places.TIERS:
            payload[tier] = _tier(
                getattr(place, f'{tier}_id'), getattr(place, f'{tier}_name'),
            )
        return payload


class BasicUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'profile_picture')


class ClubMemberSerializer(serializers.ModelSerializer):
    user = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubMember
        fields = ('id', 'user', 'role', 'joined_at')


class ClubSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()
    creator = BasicUserSerializer(read_only=True)
    visibility = serializers.SerializerMethodField()

    class Meta:
        model = Club
        fields = (
            'id', 'name', 'description', 'interest', 'emblem', 'creator',
            'created_at', 'is_active', 'member_count', 'my_role',
            'visibility_level', 'visibility',
        )
        # Set from the creator's own resolved position, never from the request
        # body — see clubs.views.ClubListCreateView.post.
        read_only_fields = ('visibility_level',)

    def get_visibility(self, obj):
        """Where this club reaches, in one object.

        Present on every club including the legacy ones, whose level is
        `global` and whose tiers are all null — a client that reads `level`
        alone still renders those correctly, as belonging nowhere in
        particular.
        """
        return {
            'level': obj.visibility_level,
            'place_name': obj.visibility_place_name,
            'city': _tier(obj.visibility_city_id, obj.visibility_city_name),
            'region': _tier(obj.visibility_region_id, obj.visibility_region_name),
            'district': _tier(obj.visibility_district_id, obj.visibility_district_name),
            'country_code': obj.visibility_country_code,
        }

    def get_member_count(self, obj):
        # `with_card_data` puts this on the row; the fallback is for the
        # single-club paths that serialise an object they just created.
        count = getattr(obj, MEMBER_COUNT_ATTR, None)
        if count is not None:
            return count
        return obj.members.count()

    def get_my_role(self, obj):
        # `hasattr`, not a None check: a null annotation is the real answer for
        # a club the viewer has not joined, and re-querying would just confirm
        # it one row at a time.
        if hasattr(obj, MY_ROLE_ATTR):
            return getattr(obj, MY_ROLE_ATTR)
        request = self.context.get('request')
        if not request:
            return None
        try:
            return obj.members.get(user=request.user).role
        except ClubMember.DoesNotExist:
            return None


class ClubDetailSerializer(ClubSerializer):
    members = ClubMemberSerializer(many=True, read_only=True)

    class Meta(ClubSerializer.Meta):
        fields = ClubSerializer.Meta.fields + ('members',)


class NumericLatLngMixin(serializers.Serializer):
    """Emits `latitude`/`longitude` as JSON numbers, not DRF's default
    stringified Decimal (COERCE_DECIMAL_TO_STRING). Fabric's typed
    view-manager props on Android refuse a string outright — AIRMapMarker
    throws "cannot be cast from String to double" rather than coercing the
    way the old bridge did — so this has to hold at the source rather than
    rely on every client normalising it on the way in.

    A club event's coordinates are optional (a proposal can be pinned to a
    named place with no map point at all), so `None` passes through rather
    than becoming `0.0` — a meetup at 0°,0° is a wrong answer, not a missing
    one.
    """
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    def get_latitude(self, obj):
        return float(obj.latitude) if obj.latitude is not None else None

    def get_longitude(self, obj):
        return float(obj.longitude) if obj.longitude is not None else None


class ClubEventSerializer(NumericLatLngMixin, serializers.ModelSerializer):
    created_by = BasicUserSerializer(read_only=True)
    approved_by = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubEvent
        fields = (
            'id', 'club', 'created_by', 'title', 'description',
            'location_name', 'latitude', 'longitude', 'event_datetime',
            'status', 'approved_by', 'approved_at', 'expires_at', 'created_at',
        )


class ClubEventNearbySerializer(NumericLatLngMixin, serializers.ModelSerializer):
    created_by = BasicUserSerializer(read_only=True)
    approved_by = BasicUserSerializer(read_only=True)
    club_name = serializers.CharField(source='club.name', read_only=True)
    club_emblem = serializers.ImageField(source='club.emblem', read_only=True)

    class Meta:
        model = ClubEvent
        fields = (
            'id', 'club', 'club_name', 'club_emblem', 'created_by',
            'title', 'description', 'location_name', 'latitude', 'longitude',
            'event_datetime', 'status', 'approved_by', 'approved_at', 'expires_at', 'created_at',
        )


class ClubMessageSerializer(serializers.ModelSerializer):
    sender = BasicUserSerializer(read_only=True)
    event = ClubEventSerializer(read_only=True)
    # Kept under the old key: clients read `image` whether the file went
    # through Django or (for messages from before the R2 migration) straight to
    # Cloudinary.
    image = serializers.SerializerMethodField()

    class Meta:
        model = ClubMessage
        fields = ('id', 'club', 'sender', 'content', 'image', 'message_type', 'event', 'created_at')

    def get_image(self, obj):
        if obj.image_url:
            return obj.image_url
        return obj.image.url if obj.image else None


#: The app's chat input stops here; both server write paths hold clients to the same limit.
MESSAGE_MAX_CHARS = 2000
#: The largest id a BigAutoField holds, so a cursor can never overflow the database parameter.
MESSAGE_ID_MAX = 2 ** 63 - 1


class ClubMessageInputSerializer(serializers.Serializer):
    """What a member may send, validated identically for the REST view and the chat socket."""
    content = serializers.CharField(max_length=MESSAGE_MAX_CHARS, allow_blank=True, default='')
    # Meetup proposals are written by the event endpoint alongside their event, never claimed by a client.
    message_type = serializers.ChoiceField(
        choices=[ClubMessage.MessageType.TEXT],
        default=ClubMessage.MessageType.TEXT,
        error_messages={'invalid_choice': 'Unsupported message_type.'},
    )


class ClubMessageCursorSerializer(serializers.Serializer):
    before = serializers.IntegerField(min_value=1, max_value=MESSAGE_ID_MAX, required=False)


def first_error(errors):
    """The first human-readable message in a DRF errors structure."""
    while isinstance(errors, (dict, list)):
        errors = next(iter(errors.values()), '') if isinstance(errors, dict) else (errors[0] if errors else '')
    return str(errors)


class ClubInvitationSerializer(serializers.ModelSerializer):
    club = serializers.SerializerMethodField()
    sender = BasicUserSerializer(read_only=True)
    receiver = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubInvitation
        fields = ('id', 'club', 'sender', 'receiver', 'status', 'created_at', 'responded_at')

    def get_club(self, obj):
        return {'id': obj.club_id, 'name': obj.club.name}
