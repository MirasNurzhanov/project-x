from django.conf import settings
from django.db import models

from . import geometry


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

    class Source(models.TextChoices):
        """Where the geometry came from.

        Matters because trust differs. An OSM boundary has been through that
        project's own review; a hand-traced one has been through nobody's until
        somebody checks it, which is what `verified` records.
        """
        OSM = 'osm', 'OpenStreetMap'
        USER = 'user', 'Traced by a user'

    class Kind(models.TextChoices):
        """How big a place this is, in the only two sizes a zone offers.

        Derived once at fetch time rather than inferred at read time, because
        "my city" and "my district" are answered from the cache and the two
        must not be confusable. Without this, a cached district would win a
        city lookup purely for being the smaller of two containing shapes.
        """
        CITY = 'city', 'City or town'
        DISTRICT = 'district', 'District within a city'
        OTHER = 'other', 'Other'

    # Both blank for a hand-traced boundary, which corresponds to no OSM object
    # at all. The uniqueness rule below is conditional for the same reason.
    osm_type = models.CharField(max_length=10, choices=OSMType.choices, blank=True)
    osm_id = models.BigIntegerField(null=True, blank=True)

    name = models.CharField(max_length=200)
    # The upstream's full label — "Almaty, Kazakhstan" — so two same-named
    # cities are still tellable apart in a picker.
    display_name = models.CharField(max_length=500)
    country_code = models.CharField(max_length=2, blank=True)
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.OTHER, db_index=True,
    )
    # OSM's own hierarchy depth: 4 is usually a state/province, 6-8 a city or
    # district. Kept for display ordering, never for logic — the meaning of a
    # given level varies by country.
    admin_level = models.PositiveSmallIntegerField(null=True, blank=True)

    # Bounding box. Every "is this point inside" test starts here: it rejects
    # almost every candidate with four float comparisons, so the ray-casting
    # walk over the rings only runs for points that are plausibly inside.
    #
    # The longitudes are *unwrapped* and so may legitimately run past ±180: a
    # region crossing the dateline is stored as e.g. 178.2 → 180.4 rather than
    # as the planet-spanning -180 → 180 that wrapping it would produce. See
    # regions.geometry for why, and `crosses_antimeridian` below for the test.
    min_lat = models.FloatField()
    min_lon = models.FloatField()
    max_lat = models.FloatField()
    max_lon = models.FloatField()

    # Wrapped back into ±180, unlike the bounds: this is only ever used to
    # frame a map, and a map wants an ordinary longitude.
    centroid_lat = models.FloatField()
    centroid_lon = models.FloatField()

    # Rings of [lon, lat] pairs — the first is the outer boundary, any others
    # are holes. Simplified before it is stored: a real city relation runs to
    # tens of thousands of points, which is more than a phone should download
    # on a poll or a map should try to draw. Unwrapped, like the bounds.
    rings = models.JSONField(default=list)
    point_count = models.PositiveIntegerField(default=0)

    # The city or municipality this sits inside, as the address hierarchy named
    # it when the region was fetched. Kept because "Central" means nothing on
    # its own and "Central, Nairobi" means a great deal — both for ranking a
    # candidate and for telling two of them apart in a picker.
    parent_name = models.CharField(max_length=200, blank=True)

    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.OSM, db_index=True,
    )
    # OSM geometry is trusted on arrival. A hand-traced boundary is trusted only
    # for the person who drew it until somebody checks it — otherwise one user
    # could redraw a district for everyone standing in it.
    verified = models.BooleanField(default=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name='submitted_regions',
    )

    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['osm_type', 'osm_id'], name='unique_osm_object',
                # Only OSM objects have an identity to be unique on. Traced
                # boundaries have none, and without this every one after the
                # first would collide on (blank, null).
                condition=models.Q(osm_id__isnull=False),
            ),
        ]
        indexes = [
            models.Index(fields=['name']),
        ]
        ordering = ['name']

    def __str__(self):
        return self.display_name or self.name

    @property
    def crosses_antimeridian(self) -> bool:
        """Whether this region's window runs over the dateline.

        Derived rather than stored: it is a fact about the bounds, and a column
        could drift out of step with them.
        """
        return geometry.crosses_antimeridian(self.min_lon, self.max_lon)

    def contains(self, lat, lon) -> bool:
        """Whether a point falls inside this region.

        Bounding box first, then even-odd ray casting per ring: a point is
        inside when it is within the outer ring an odd number of times overall,
        which makes holes fall out for free rather than needing a second pass.

        `lon` arrives as an ordinary ±180 longitude and is aligned to this
        region's window before anything is compared — see regions.geometry.
        """
        return geometry.contains(
            self.rings,
            self.min_lat, self.min_lon, self.max_lat, self.max_lon,
            lat, lon,
        )

    def could_contain(self, lat, lon, tolerance_m) -> bool:
        """`contains`, allowing for a position we know to be imprecise.

        Used where the coordinates being tested are the ones *stored* for a
        user rather than the ones they are standing on — see
        geometry.contains_within for why those differ.
        """
        return geometry.contains_within(
            self.rings,
            self.min_lat, self.min_lon, self.max_lat, self.max_lon,
            lat, lon, tolerance_m,
        )
