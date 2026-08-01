from django.db import models


class Region(models.Model):
    """A named administrative area — a city, district, province — cached from
    OpenStreetMap so a zone can follow real borders instead of a circle.

    Rows are a *cache*, not user data: one per place, shared by everyone whose
    zone covers it. Nothing here is ever edited by a user, and deleting a row
    only costs the next person who picks that place one upstream lookup.
    """

    class OSMType(models.TextChoices):
        NODE = 'node', 'Node'
        WAY = 'way', 'Way'
        RELATION = 'relation', 'Relation'

    osm_type = models.CharField(max_length=10, choices=OSMType.choices)
    osm_id = models.BigIntegerField()

    name = models.CharField(max_length=200)
    # The upstream's full label — "Almaty, Kazakhstan" — so two same-named
    # cities are still tellable apart in a picker.
    display_name = models.CharField(max_length=500)
    country_code = models.CharField(max_length=2, blank=True)
    # OSM's own hierarchy depth: 4 is usually a state/province, 6-8 a city or
    # district. Kept for display ordering, never for logic — the meaning of a
    # given level varies by country.
    admin_level = models.PositiveSmallIntegerField(null=True, blank=True)

    # Bounding box. Every "is this point inside" test starts here: it rejects
    # almost every candidate with four float comparisons, so the ray-casting
    # walk over the rings only runs for points that are plausibly inside.
    min_lat = models.FloatField()
    min_lon = models.FloatField()
    max_lat = models.FloatField()
    max_lon = models.FloatField()

    centroid_lat = models.FloatField()
    centroid_lon = models.FloatField()

    # Rings of [lon, lat] pairs — the first is the outer boundary, any others
    # are holes. Simplified before it is stored: a real city relation runs to
    # tens of thousands of points, which is more than a phone should download
    # on a poll or a map should try to draw.
    rings = models.JSONField(default=list)
    point_count = models.PositiveIntegerField(default=0)

    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['osm_type', 'osm_id'], name='unique_osm_object',
            ),
        ]
        indexes = [
            models.Index(fields=['name']),
        ]
        ordering = ['name']

    def __str__(self):
        return self.display_name or self.name

    def contains(self, lat, lon) -> bool:
        """Whether a point falls inside this region.

        Bounding box first, then even-odd ray casting per ring: a point is
        inside when it is within the outer ring an odd number of times overall,
        which makes holes fall out for free rather than needing a second pass.
        """
        if not (self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon):
            return False

        inside = False
        for ring in self.rings:
            if _ring_contains(ring, lat, lon):
                inside = not inside
        return inside


def _ring_contains(ring, lat, lon) -> bool:
    """Even-odd ray cast against one ring of [lon, lat] pairs.

    Counts crossings of a ray running east from the point. The `(y1 > lat) !=
    (y2 > lat)` test picks exactly one of the two edges meeting at a vertex, so
    a ray passing precisely through a vertex is not counted twice.
    """
    inside = False
    count = len(ring)
    if count < 3:
        return False

    j = count - 1
    for i in range(count):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[j][0], ring[j][1]
        if (y1 > lat) != (y2 > lat):
            # x of the edge where it crosses this latitude
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
        j = i
    return inside
