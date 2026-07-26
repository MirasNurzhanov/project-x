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

        # A private account is a pin and a name until they approve you.
        if visibility.minimal_only(me, obj, rel):
            return data

        data['bio'] = obj.bio

        # Zone *presence* stays visible to everyone so the map can still draw
        # the circle; only the audio itself is gated.
        data['zone_radius'] = obj.zone_radius
        data['zone_active'] = obj.zone_active

        if visibility.can_view(me, obj, 'interests', rel):
            data['interests'] = InterestSerializer(obj.interests.all(), many=True).data
            mine = self.context.get('my_interest_ids', frozenset())
            data['shared_interests'] = [i.name for i in obj.interests.all() if i.pk in mine]

        if visibility.can_view(me, obj, 'zone_audio', rel):
            data['zone_audio'] = obj.zone_audio.url if obj.zone_audio else None
            data['zone_audio_url'] = _preset_or_upload_url(obj)

        return data


def _preset_or_upload_url(obj):
    if obj.zone_preset and obj.zone_preset in PRESET_MAP:
        return PRESET_MAP[obj.zone_preset]['url']
    if obj.zone_audio:
        return obj.zone_audio.url
    return None
