import logging
from django.utils.functional import cached_property
from rest_framework import serializers
from users import visibility
from users.models import User
from interests.serializers import InterestSerializer
logger = logging.getLogger(__name__)


class NotOnMap(Exception):
    """Raised when a row reaches NearbyUserSerializer that should not be drawn.

    Never expected — the querysets filter on it — so it is a bug signal rather
    than control flow. It is caught one level up and the row is dropped, which
    fails closed: the person stays off the map, and the rest of the map still
    draws.
    """


class LocationUpdateSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, min_value=-90, max_value=90
    )
    longitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, min_value=-180, max_value=180
    )
    #: Metres per second, straight from the platform's fused location provider
    #: (expo-location's `coords.speed`), not derived from successive fixes.
    #: Accepted, bucketed, and thrown away — never stored, never echoed back.
    #: expo-location reports -1 when it has no speed estimate.
    speed = serializers.FloatField(required=False, allow_null=True, min_value=-1)
    #: Radius of the phone's own confidence in this fix, in metres. Unlike
    #: `speed` this *is* stored, because anything deciding what a user may see
    #: from where they are has to know how well the position is known — a fix
    #: with a two-kilometre radius cannot place anyone in a district.
    #: expo-location reports -1 when it has no estimate.
    accuracy = serializers.FloatField(required=False, allow_null=True, min_value=-1)


class NearbyQuerySerializer(serializers.Serializer):
    """The anchor for one private discovery request, and nothing else.

    Deliberately narrower than LocationUpdateSerializer: no `speed`, no
    `accuracy`. Those exist because the update endpoint *stores* what it is
    given, and this one stores nothing — a field accepted here could only ever
    be a field somebody later persisted by mistake. If a future reader wants
    accuracy for this path, the honest move is to add it and prove nothing
    writes it, not to have left the door open.
    """

    latitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, min_value=-90, max_value=90
    )
    longitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, min_value=-180, max_value=180
    )


class _NearbyUserListSerializer(serializers.ListSerializer):
    """Drops rows the child refuses, instead of failing the whole response.

    One person who slipped past a queryset filter must not blank the map for
    the viewer, and must not be drawn either. Dropping does both.
    """

    def to_representation(self, data):
        out = []
        for item in data:
            try:
                out.append(self.child.to_representation(item))
            except NotOnMap as exc:
                logger.warning('map_paused_row_dropped')
        return out


class NearbyUserSerializer(serializers.Serializer):
    """A person on the map, assembled field by field.

    Hidden fields are omitted from the payload rather than nulled — the map
    used to send `zone_audio_url` to everyone and rely on the client to hide
    it, which is exactly the failure mode this shape prevents.

    Coordinates are **not** whatever is stored. The database holds the precise
    point for everybody, because friends are entitled to it; what a given
    viewer may see is decided here, per row, by `visibility.coordinates_for`.
    Reaching for `obj.latitude` in this file would hand a stranger the truth.
    """

    @cached_property
    def _interests(self):
        """One InterestSerializer for the whole response, not one per person.

        `many=True` wraps a single child instance and calls it once per row, so
        caching on `self` means built once per request. It is worth the
        indirection: constructing a ModelSerializer makes it introspect the
        model and rebuild its field map, and doing that per person was a
        quarter of the wall time of this endpoint — more than the JSON encoder,
        the gzip pass and the middleware chain put together.

        Deliberately built without the request context. `is_selected` on that
        serializer answers "has the viewer picked this interest" with a query
        per interest, and the map has never sent anything but `false` for it —
        supplying the context here would be an N+1 dressed as a tidy-up.
        """
        return InterestSerializer()

    class Meta:
        list_serializer_class = _NearbyUserListSerializer

    def to_representation(self, obj):
        me = self.context['request'].user
        rel = self.context.get('relationships', {}).get(obj.pk, visibility.STRANGER)

        # The querysets feeding this serializer all go through
        # `visible_users(on_map=True)`, so no ghost should ever reach here.
        # Checked rather than assumed: this is the last line before coordinates
        # leave the process, and a future caller assembling its own list is
        # exactly the mistake that would put someone back on the map without
        # their say-so. Raising would take the whole map down for one bad row,
        # so the row is dropped instead — see `_NearbyUserListSerializer`.
        if me.ghost_mode or not obj.is_on_map():
            raise NotOnMap(obj.pk)

        # Precision is the viewer's relationship to this row, not a setting on
        # it. `friend_ids` comes down in the context already built by the view;
        # without it this would be a query per person on the map.
        friend_ids = self.context.get('friend_ids')
        latitude, longitude = visibility.coordinates_for(me, obj, friend_ids)
        precise = visibility.is_friend(me, obj, friend_ids)

        data = {
            'id': obj.id,
            'username': obj.username,
            'profile_picture': obj.profile_picture.url if obj.profile_picture else None,
            'is_private': obj.is_private,
            'relationship': rel,
            'latitude': latitude,
            'longitude': longitude,
            # Derived from the same fuzzed point the pin uses — see the view. A
            # coarse pin beside a precise distance is a precise pin.
            'distance_km': self.context.get('distances', {}).get(obj.pk),
            'location_precision': 'precise' if precise else 'approximate',
        }

        # Presence sits above the private-account gate because `can_view`
        # already refuses it to non-approved viewers of a private account. It
        # is omitted entirely when hidden — never sent as a grey "offline",
        # which would be both false and a tell that they are hiding.
        presence = visibility.presence_for(me, obj, rel)
        if presence is not None:
            data['presence'] = presence

        motion = visibility.motion_for(me, obj, rel)
        if motion is not None:
            data['motion_class'] = motion

        # A private account is a pin and a name until they approve you.
        if visibility.minimal_only(me, obj, rel):
            return data

        data['bio'] = obj.bio
        from users.ages import age_in_years
        data['age'] = age_in_years(obj.date_of_birth)
        data['nationality'] = obj.nationality
        data['profession'] = obj.profession

        # Zone *presence* stays visible to everyone so the map can still draw
        # the shape; only the audio itself is gated.
        data['zone_radius'] = obj.zone_radius
        data['zone_active'] = obj.zone_active
        # What the zone *is*. Authoritative, and the same value the containment
        # service branches on, so a client can never be told one shape while the
        # backend tests another.
        data['zone_type'] = obj.zone_type
        # What to *draw*, which is a coarser question — both polygon types draw
        # identically, and a region evicted from the cache has to fall back to
        # something renderable or the map shows nothing at all. Deliberately not
        # the same value as `zone_type`: this one is allowed a fallback because
        # it only decides pixels. See User.effective_zone_mode.
        data['zone_mode'] = obj.effective_zone_mode
        # The id only. This payload is polled every 10 seconds by every client,
        # and a city outline is ~1500 points — the map resolves the id against
        # /api/regions/<id>/ once and caches it, however many people share it.
        data['zone_region_id'] = obj.zone_region_id

        if visibility.can_view(me, obj, 'interests', rel):
            # One pass over the prefetched interests, feeding both keys.
            interests = obj.interests.all()
            data['interests'] = [self._interests.to_representation(i) for i in interests]
            mine = self.context.get('my_interest_ids', frozenset())
            data['shared_interests'] = [i.name for i in interests if i.pk in mine]

        # `music_zone_active` on top of the visibility check, and the whole
        # block behind it. A switched-off music zone withholds the track rather
        # than nulling it, for the same reason the rest of this serializer
        # omits: a listener sent a radius and no URL is a client deciding
        # whether to draw a ring around silence, which is the client's
        # judgement to get wrong. Nothing here is sent while the music zone is
        # off, and that absence is what takes the circle off every viewer's map.
        if visibility.can_view(me, obj, 'zone_audio', rel) and obj.music_zone_active:
            data['zone_audio'] = obj.zone_audio.url if obj.zone_audio else None
            data['zone_audio_url'] = _zone_track_url(obj)
            # How close you have to be to hear it, which is not the same as how
            # big their zone is. Sent inside this gate rather than with
            # `zone_radius` above: to someone who may not hear the track, the
            # music zone is a shape with no meaning.
            data['music_zone_radius'] = obj.music_zone_radius
            # Apple tracks have a real name and cover art. Without these the
            # map would fall back to parsing a title out of the preview URL's
            # filename, which for Apple's CDN is a hash.
            if obj.zone_apple_preview_url:
                data['zone_track_title'] = obj.zone_apple_title
                data['zone_track_artist'] = obj.zone_apple_artist
                data['zone_track_artwork'] = obj.zone_apple_artwork
            # The slice that loops. Sent to listeners, not just the owner — the
            # trim is part of what the zone plays, so a card that ignored it
            # would play a different thing from what its owner set up.
            if obj.has_trimmed_loop:
                data['zone_track_start_ms'] = obj.zone_track_start_ms
                data['zone_track_end_ms'] = obj.zone_track_end_ms

        return data


def _zone_track_url(obj):
    if obj.zone_apple_preview_url:
        return obj.zone_apple_preview_url
    if obj.zone_audio:
        return obj.zone_audio.url
    return None
