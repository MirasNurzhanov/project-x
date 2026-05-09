from rest_framework import serializers
from users.models import User
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

    class Meta:
        model = User
        fields = (
            'id', 'username', 'profile_picture', 'bio', 'interests',
            'latitude', 'longitude', 'distance_km',
            'zone_audio', 'zone_radius', 'zone_active',
        )

    def get_distance_km(self, obj):
        return self.context.get('distances', {}).get(obj.pk)
