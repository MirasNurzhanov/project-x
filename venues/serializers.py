from rest_framework import serializers
from .models import Venue


class VenueSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)

    class Meta:
        model = Venue
        fields = ('id', 'name', 'slug', 'address', 'category', 'description',
                  'image_url', 'latitude', 'longitude')
