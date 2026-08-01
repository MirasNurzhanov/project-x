from rest_framework import serializers

from .models import Region


class RegionSummarySerializer(serializers.ModelSerializer):
    """Everything but the geometry.

    This is what a picker list and the nearby feed carry. The rings are the
    expensive part of a region by two orders of magnitude, and neither of those
    surfaces draws the outline — the map fetches it once per region, by id.
    """

    class Meta:
        model = Region
        fields = (
            'id', 'name', 'display_name', 'country_code', 'admin_level',
            'min_lat', 'min_lon', 'max_lat', 'max_lon',
            'centroid_lat', 'centroid_lon', 'point_count',
        )


class RegionSerializer(RegionSummarySerializer):
    """The summary plus the outline itself, for drawing."""

    class Meta(RegionSummarySerializer.Meta):
        fields = RegionSummarySerializer.Meta.fields + ('rings',)
