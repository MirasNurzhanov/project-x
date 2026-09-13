from django.conf import settings
from rest_framework import serializers

from . import geometry
from .models import Region


class _BoundedListField(serializers.ListField):
    """A ListField that refuses an oversized list before validating a single item of it."""

    default_error_messages = {'too_many_items': 'Ensure this field has no more than {max_items} elements.'}

    def __init__(self, *args, max_items=None, **kwargs):
        self.max_items = max_items
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        if self.max_items is not None and isinstance(data, list) and len(data) > self.max_items:
            self.fail('too_many_items', max_items=self.max_items)
        return super().to_internal_value(data)


class RegionSummarySerializer(serializers.ModelSerializer):
    """Everything but the geometry.

    This is what a picker list and the nearby feed carry. The rings are the
    expensive part of a region by two orders of magnitude, and neither of those
    surfaces draws the outline — the map fetches it once per region, by id.
    """

    class Meta:
        model = Region
        fields = (
            'id', 'name', 'display_name', 'country_code', 'kind', 'admin_level',
            'min_lat', 'min_lon', 'max_lat', 'max_lon',
            'centroid_lat', 'centroid_lon', 'point_count',
        )


class RegionSerializer(RegionSummarySerializer):
    """The summary plus the outline itself, for drawing."""

    class Meta(RegionSummarySerializer.Meta):
        fields = RegionSummarySerializer.Meta.fields + ('rings',)


class CandidateSerializer(serializers.Serializer):
    """One ranked choice, for when the pipeline will not pick on its own.

    The score and the reasons ride along because a picker that says "Msasani"
    and "Kinondoni" with nothing to tell them apart has moved the problem to
    the user rather than solved it. The reasons are what make the ranking
    legible: *sits inside Dar es Salaam*, *admin level 7 is primary for tz*.
    """

    region = RegionSummarySerializer()
    score = serializers.IntegerField()
    reasons = serializers.ListField(child=serializers.CharField())
    stage = serializers.CharField()


class TracedRegionSerializer(serializers.Serializer):
    """A boundary somebody drew by hand, on its way in.

    Deliberately not a ModelSerializer: almost nothing a client sends maps
    directly onto the row. The bounds, the centroid, the point count, the
    source and the trust flag are all derived here, because a client that could
    set them could claim a traced shape was an OSM one.
    """

    name = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(choices=[Region.Kind.CITY, Region.Kind.DISTRICT])
    parent_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default='',
    )
    # Every level is bounded before its items are parsed, so an oversized body costs a length check, not a float parse.
    rings = _BoundedListField(
        child=_BoundedListField(
            child=_BoundedListField(child=serializers.FloatField(), min_length=2, max_items=2),
            min_length=3,
            error_messages={'too_many_items': 'Each ring may have at most {max_items} points.'},
        ),
        min_length=1,
        max_items=8,
        error_messages={'too_many_items': 'An outline may have at most {max_items} rings.'},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['rings'].child.max_items = settings.REGION_MAX_TRACED_RING_POINTS

    def validate_rings(self, rings):
        """Unwrap, measure, and refuse anything that is not a plausible area.

        The size checks are the substance. A traced boundary exists because
        something is too small or too new to be in OSM, so a submission the size
        of a province is either a mistake or an attempt to blanket a region, and
        neither should be stored.
        """
        max_points = settings.REGION_MAX_TRACED_POINTS
        if sum(len(ring) for ring in rings) > max_points:
            raise serializers.ValidationError(
                f'That outline has too many points. Keep it to {max_points} in total.'
            )

        for ring in rings:
            for lon, lat in ring:
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    raise serializers.ValidationError(
                        'Coordinates must be [longitude, latitude] within range.'
                    )

        unwrapped = geometry.unwrap_rings(rings)
        if not unwrapped:
            raise serializers.ValidationError('That outline encloses nothing.')

        min_lat, min_lon, max_lat, max_lon = geometry.bounds(unwrapped)
        area = (max_lat - min_lat) * (max_lon - min_lon)
        if area <= 0:
            raise serializers.ValidationError('That outline encloses no area.')

        limit = self.context.get('max_area')
        if limit and area > limit:
            raise serializers.ValidationError(
                'That outline is far too large for a district. Trace just the '
                'area itself.'
            )
        # ~50m across at the equator. Below this it is a pin, not a boundary.
        if max(max_lat - min_lat, max_lon - min_lon) < 0.0005:
            raise serializers.ValidationError(
                'That outline is too small to be an area.'
            )

        self.context['unwrapped'] = unwrapped
        self.context['bounds'] = (min_lat, min_lon, max_lat, max_lon)
        return rings
