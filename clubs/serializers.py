from rest_framework import serializers

from regions import places
from users.models import User
from .models import Club, ClubEvent, ClubMember, ClubMessage


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
        return obj.members.count()

    def get_my_role(self, obj):
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


class ClubEventSerializer(serializers.ModelSerializer):
    created_by = BasicUserSerializer(read_only=True)
    approved_by = BasicUserSerializer(read_only=True)

    class Meta:
        model = ClubEvent
        fields = (
            'id', 'club', 'created_by', 'title', 'description',
            'location_name', 'latitude', 'longitude', 'event_datetime',
            'status', 'approved_by', 'approved_at', 'expires_at', 'created_at',
        )


class ClubEventNearbySerializer(serializers.ModelSerializer):
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

    class Meta:
        model = ClubMessage
        fields = ('id', 'club', 'sender', 'content', 'image', 'message_type', 'event', 'created_at')
