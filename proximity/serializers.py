from rest_framework import serializers
from users.models import User
from users.presets import PRESET_MAP
from interests.serializers import InterestSerializer


class LocationUpdateSerializer(serializers.Serializer):
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-90, max_value=90
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-180, max_value=180
    )


class NearbyUserSerializer(serializers.ModelSerializer):
    interests = InterestSerializer(many=True, read_only=True)
    distance_km = serializers.SerializerMethodField()
    zone_audio_url = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    relationship = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'username', 'profile_picture', 'bio', 'interests',
            'latitude', 'longitude', 'distance_km',
            'zone_audio', 'zone_audio_url', 'zone_radius', 'zone_active',
            'relationship',
        )

    def get_latitude(self, obj):
        return None if obj.privacy_mode == 'ghost' else obj.latitude

    def get_longitude(self, obj):
        return None if obj.privacy_mode == 'ghost' else obj.longitude

    def get_distance_km(self, obj):
        if obj.privacy_mode == 'ghost':
            return None
        return self.context.get('distances', {}).get(obj.pk)

    def get_relationship(self, obj):
        return self.context.get('relationships', {}).get(obj.pk, 'stranger')

    def get_zone_audio_url(self, obj):
        if obj.zone_preset and obj.zone_preset in PRESET_MAP:
            return PRESET_MAP[obj.zone_preset]['url']
        if obj.zone_audio:
            return obj.zone_audio.url
        return None
