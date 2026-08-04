from rest_framework import serializers
from users import visibility
from users.models import User
from users.presets import PRESET_MAP
from interests.serializers import InterestSerializer


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


class NearbyUserSerializer(serializers.Serializer):
    """A person on the map, assembled field by field.

    Hidden fields are omitted from the payload rather than nulled — the map
    used to send `zone_audio_url` to everyone and rely on the client to hide
    it, which is exactly the failure mode this shape prevents.

    Coordinates are whatever is stored. Users on Approximate never have a
    precise position in the database at all (see users.visibility), so there is
    nothing here to redact.
    """

    def to_representation(self, obj):
        me = self.context['request'].user
        rel = self.context.get('relationships', {}).get(obj.pk, visibility.STRANGER)

        data = {
            'id': obj.id,
            'username': obj.username,
            'profile_picture': obj.profile_picture.url if obj.profile_picture else None,
            'is_private': obj.is_private,
            'relationship': rel,
            'latitude': obj.latitude,
            'longitude': obj.longitude,
            'distance_km': self.context.get('distances', {}).get(obj.pk),
            'location_precision': (
                'approximate'
                if obj.location_sharing == User.LocationSharing.APPROXIMATE
                else 'precise'
            ),
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
        data['date_of_birth'] = obj.date_of_birth
        data['nationality'] = obj.nationality
        data['profession'] = obj.profession

        # Zone *presence* stays visible to everyone so the map can still draw
        # the shape; only the audio itself is gated.
        data['zone_radius'] = obj.zone_radius
        data['zone_active'] = obj.zone_active
        # `effective_` rather than the raw mode: a region that has been evicted
        # from the cache would otherwise tell the map to draw a polygon it can
        # never fetch, and it would silently render no zone at all.
        data['zone_mode'] = obj.effective_zone_mode
        # The id only. This payload is polled every 10 seconds by every client,
        # and a city outline is ~1500 points — the map resolves the id against
        # /api/regions/<id>/ once and caches it, however many people share it.
        data['zone_region_id'] = obj.zone_region_id

        if visibility.can_view(me, obj, 'interests', rel):
            data['interests'] = InterestSerializer(obj.interests.all(), many=True).data
            mine = self.context.get('my_interest_ids', frozenset())
            data['shared_interests'] = [i.name for i in obj.interests.all() if i.pk in mine]

        if visibility.can_view(me, obj, 'zone_audio', rel):
            data['zone_audio'] = obj.zone_audio.url if obj.zone_audio else None
            data['zone_audio_url'] = _zone_track_url(obj)
            # How close you have to be to hear it, which is no longer the same
            # as how big their zone is. Sent inside this gate rather than with
            # `zone_radius` above: to someone who may not hear the track, the
            # earshot circle is a shape with no meaning.
            data['music_zone_radius'] = obj.music_radius_m
            data['music_zone_separate'] = obj.music_zone_separate
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
    if obj.zone_preset and obj.zone_preset in PRESET_MAP:
        return PRESET_MAP[obj.zone_preset]['url']
    if obj.zone_audio:
        return obj.zone_audio.url
    return None
