"""Containment for the three zone types.

These are the tests that decide whether city and district zones are real. A
polygon zone that draws correctly and is tested as a circle is exactly the bug
this suite exists to catch, so every case here asserts on
`User.zone_contains` — the containment service — rather than on anything the
API happens to serialise.

Geometry is kept deliberately crude and hand-written. A real OSM city relation
is tens of thousands of points and proves nothing a unit square does not; what
needs proving is that the *type* selects the test, that polygons beat distance,
and that a missing boundary is an error rather than a quiet circle.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from regions.models import Region
from users.models import User
from users import zones


#: One degree of latitude is ~111km, so a 0.02° square is ~2.2km on a side —
#: comfortably larger than any radius the circle type allows (5km max is the
#: cap, and the circles here are 500m). That gap is the point: a point can sit
#: well inside the polygon and far outside any circle, which is what tells the
#: two implementations apart.
def make_region(name='Testville', kind=Region.Kind.CITY, rings=None,
                min_lat=0.0, min_lon=0.0, max_lat=1.0, max_lon=1.0):
    return Region.objects.create(
        name=name,
        display_name=f'{name}, Testland',
        kind=kind,
        rings=rings if rings is not None else [
            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
        ],
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=(min_lat + max_lat) / 2, centroid_lon=(min_lon + max_lon) / 2,
    )


def make_user(username, **kwargs):
    return User.objects.create_user(
        username=username, email=f'{username}@example.com', password='testpass-123',
        **kwargs,
    )


class CircleZoneTests(TestCase):
    """Type 1: distance from the user's own position."""

    def setUp(self):
        # 0.005° of latitude ≈ 555m, so the fixtures below sit either side of a
        # 500m radius by a comfortable margin rather than on the edge of it.
        self.user = make_user(
            'circle-owner', latitude='10.0', longitude='10.0',
            zone_type=User.ZoneType.CIRCLE, zone_radius=500,
        )

    def test_a_point_inside_the_radius_is_inside(self):
        # ~300m north.
        self.assertTrue(self.user.zone_contains(10.0027, 10.0))

    def test_a_point_outside_the_radius_is_outside(self):
        # ~800m north.
        self.assertFalse(self.user.zone_contains(10.0072, 10.0))

    def test_the_radius_is_the_users_own_not_a_hardcoded_500(self):
        """The dial has to be what is tested. A fixed 500m anywhere in the
        containment path would pass the two tests above and fail this one."""
        self.user.zone_radius = 2000
        self.assertTrue(self.user.zone_contains(10.0072, 10.0))     # ~800m
        self.assertFalse(self.user.zone_contains(10.025, 10.0))     # ~2.8km

    def test_a_circle_with_no_position_raises(self):
        self.user.latitude = None
        self.user.longitude = None
        with self.assertRaises(zones.ZoneBoundaryUnavailable):
            self.user.zone_contains(10.0, 10.0)


class PolygonZoneTests(TestCase):
    """Types 2 and 3: point-in-polygon, and never distance."""

    def setUp(self):
        self.city = make_region('Metropolis', Region.Kind.CITY)
        self.district = make_region(
            'Old Town', Region.Kind.DISTRICT,
            rings=[[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]]],
            min_lat=0.0, min_lon=0.0, max_lat=0.2, max_lon=0.2,
        )
        # Positioned in a corner of the city, so "far from the centre" is a
        # property of the fixture rather than something a test has to arrange.
        self.user = make_user(
            'polygon-owner', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=self.city, zone_radius=500,
        )

    def test_a_point_inside_the_district_polygon_is_inside(self):
        self.user.zone_type = User.ZoneType.DISTRICT
        self.user.zone_region = self.district
        self.assertTrue(self.user.zone_contains(0.1, 0.1))

    def test_a_close_point_outside_the_district_boundary_is_outside(self):
        """Just over the line, and well within any circle — distance-based
        containment would call this inside."""
        self.user.zone_type = User.ZoneType.DISTRICT
        self.user.zone_region = self.district
        self.assertFalse(self.user.zone_contains(0.25, 0.1))

    def test_a_point_far_from_the_city_centre_is_still_inside_the_city(self):
        """The headline case. ~100km from the centroid and unmistakably inside
        the boundary — a 500m circle says no, the polygon says yes."""
        self.assertTrue(self.user.zone_contains(0.95, 0.95))

    def test_a_point_just_outside_the_city_is_outside(self):
        self.assertFalse(self.user.zone_contains(1.05, 0.5))

    def test_a_polygon_zone_with_no_boundary_raises_rather_than_shrinking(self):
        """The regression that matters most: `zone_region` is SET_NULL, so a
        city zone really can end up with nothing to cover. It must not quietly
        become the 500m circle whose radius is still sitting in the row."""
        self.user.zone_region = None
        with self.assertRaises(zones.ZoneBoundaryUnavailable) as ctx:
            self.user.zone_contains(0.1, 0.1)
        self.assertEqual(ctx.exception.code, 'boundary_not_available')

    def test_the_stored_radius_is_ignored_entirely(self):
        """A city zone carries a radius it is not allowed to use."""
        self.user.zone_radius = 50
        self.assertTrue(self.user.zone_contains(0.95, 0.95))


class ZoneTypeChangeTests(TestCase):
    """Switching type changes behaviour immediately, in both directions."""

    def setUp(self):
        self.city = make_region('Metropolis', Region.Kind.CITY)
        self.district = make_region(
            'Old Town', Region.Kind.DISTRICT,
            rings=[[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]]],
            min_lat=0.0, min_lon=0.0, max_lat=0.2, max_lon=0.2,
        )
        self.user = make_user(
            'switcher', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CIRCLE, zone_radius=500, zone_region=self.city,
        )
        # ~11km from the user: outside any circle, inside the city, outside the
        # district. One point that gives a different answer for all three types.
        self.probe = (0.2, 0.3)

    def test_circle_to_city_stops_using_the_radius(self):
        self.assertFalse(self.user.zone_contains(*self.probe))
        self.user.zone_type = User.ZoneType.CITY
        self.assertTrue(self.user.zone_contains(*self.probe))

    def test_city_to_district_changes_containment(self):
        self.user.zone_type = User.ZoneType.CITY
        self.assertTrue(self.user.zone_contains(*self.probe))
        self.user.zone_type = User.ZoneType.DISTRICT
        self.user.zone_region = self.district
        self.assertFalse(self.user.zone_contains(*self.probe))

    def test_district_to_circle_restores_radius_behaviour(self):
        self.user.zone_type = User.ZoneType.DISTRICT
        self.user.zone_region = self.district
        self.assertTrue(self.user.zone_contains(0.1, 0.1))
        self.user.zone_type = User.ZoneType.CIRCLE
        self.assertTrue(self.user.zone_contains(0.1, 0.1))          # still, by distance
        self.assertFalse(self.user.zone_contains(*self.probe))      # but not 11km out


class PolygonEdgeCaseTests(TestCase):
    """MultiPolygon, holes, and points on the line."""

    def test_a_hole_is_not_inside(self):
        """Even-odd ray casting gets holes for free — this pins that it does."""
        region = make_region(
            'Ringtown', Region.Kind.CITY,
            rings=[
                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
                [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]],
            ],
        )
        user = make_user(
            'holed', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertTrue(user.zone_contains(0.2, 0.2))    # in the ring
        self.assertFalse(user.zone_contains(0.5, 0.5))   # in the hole

    def test_a_point_on_an_island_of_a_multipolygon_is_inside(self):
        """A city with a detached island. The second outer ring has to count."""
        region = make_region(
            'Archipelago', Region.Kind.CITY,
            rings=[
                [[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]],
                [[0.8, 0.8], [1.0, 0.8], [1.0, 1.0], [0.8, 1.0], [0.8, 0.8]],
            ],
        )
        user = make_user(
            'islander', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertTrue(user.zone_contains(0.1, 0.1))    # mainland
        self.assertTrue(user.zone_contains(0.9, 0.9))    # island
        self.assertFalse(user.zone_contains(0.5, 0.5))   # the sea between them

    def test_a_point_on_the_boundary_is_inside(self):
        """ST_Covers semantics, not ST_Contains: the border belongs to the
        region. Left to the raw ray cast the answer is whatever happens at that
        vertex, which lets the two sides of a shared border disagree about who
        is standing on it."""
        region = make_region('Edgeville', Region.Kind.CITY)
        user = make_user(
            'edger', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertTrue(user.zone_contains(0.0, 0.5))    # on an edge
        self.assertTrue(user.zone_contains(0.0, 0.0))    # on a vertex
        self.assertTrue(user.zone_contains(1.0, 1.0))    # opposite vertex

    def test_a_point_on_a_holes_edge_is_inside(self):
        """A hole's rim is part of the polygon's boundary, so it is covered —
        even though a point a hair further in is not."""
        region = make_region(
            'Ringtown', Region.Kind.CITY,
            rings=[
                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
                [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]],
            ],
        )
        user = make_user(
            'rimmer', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertTrue(user.zone_contains(0.4, 0.5))     # on the hole's rim
        self.assertFalse(user.zone_contains(0.5, 0.5))    # inside the hole

    def test_boundary_inclusion_does_not_leak_outside(self):
        """The edge test must not widen the region by anything visible. A metre
        out is still out — ~9e-6 degrees, two orders above the epsilon."""
        region = make_region('Edgeville', Region.Kind.CITY)
        user = make_user(
            'precise', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertFalse(user.zone_contains(-0.00001, 0.5))

    def test_an_empty_ring_list_is_outside_not_an_error(self):
        """A Region row with no geometry is a cache miss, not a crash. It has a
        boundary column, so containment answers rather than raising — and the
        answer is 'outside', because nothing is inside nothing."""
        region = make_region('Emptyville', Region.Kind.CITY, rings=[])
        user = make_user(
            'empty', latitude='0.1', longitude='0.1',
            zone_type=User.ZoneType.CITY, zone_region=region,
        )
        self.assertFalse(user.zone_contains(0.5, 0.5))


class ZoneApiTests(TestCase):
    """The type survives a round trip, and invalid combinations are refused."""

    def setUp(self):
        self.city = make_region('Metropolis', Region.Kind.CITY)
        self.district = make_region(
            'Old Town', Region.Kind.DISTRICT,
            rings=[[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]]],
            min_lat=0.0, min_lon=0.0, max_lat=0.2, max_lon=0.2,
        )
        self.user = make_user('api-user', latitude='0.1', longitude='0.1')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_zone_type_round_trips(self):
        r = self.client.patch(
            '/api/users/zone/',
            {'zone_type': 'city', 'zone_region': self.city.pk},
            format='json',
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['zone_type'], 'city')
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.CITY)

    def test_a_polygon_type_without_a_boundary_is_refused(self):
        r = self.client.patch('/api/users/zone/', {'zone_type': 'city'}, format='json')
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertIn('zone_region', body)
        # A code the client can branch on, so "we could not resolve a boundary"
        # can be told apart from an ordinary validation message without
        # string-matching English.
        self.assertEqual(body['code'], ['boundary_not_available'])

    def test_a_district_type_cannot_point_at_a_city_boundary(self):
        """Otherwise 'district' is just a word the phone sent, and containment
        would honour a whole-city polygon while the API said district."""
        r = self.client.patch(
            '/api/users/zone/',
            {'zone_type': 'district', 'zone_region': self.city.pk},
            format='json',
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('zone_region', r.json())

    def test_a_city_type_cannot_point_at_a_district_boundary(self):
        r = self.client.patch(
            '/api/users/zone/',
            {'zone_type': 'city', 'zone_region': self.district.pk},
            format='json',
        )
        self.assertEqual(r.status_code, 400)

    def test_an_other_region_is_refused_for_both_types(self):
        """`other` is what the classifier says when a place is neither of the
        two sizes offered — a province or a country. Accepting one as a city
        zone would let somebody cover a whole province, and containment would
        honour it exactly."""
        province = make_region('Big Province', Region.Kind.OTHER)
        for zone_type in ('city', 'district'):
            with self.subTest(zone_type=zone_type):
                r = self.client.patch(
                    '/api/users/zone/',
                    {'zone_type': zone_type, 'zone_region': province.pk},
                    format='json',
                )
                self.assertEqual(r.status_code, 400)
                self.assertIn('zone_region', r.json())

    def test_switching_to_circle_needs_no_boundary(self):
        self.user.zone_type = User.ZoneType.CITY
        self.user.zone_region = self.city
        self.user.save()
        r = self.client.patch(
            '/api/users/zone/', {'zone_type': 'circle', 'zone_radius': 800}, format='json',
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.CIRCLE)
        # ~550m out: inside the new dial, and nothing to do with the city that
        # is still sitting in `zone_region`. The far probe is inside that city,
        # so it also pins that the polygon stopped being consulted.
        self.assertTrue(self.user.zone_contains(0.105, 0.1))
        self.assertFalse(self.user.zone_contains(0.2, 0.3))

    def test_the_drawing_hint_falls_back_but_containment_does_not(self):
        """`effective_zone_mode` is allowed to degrade to a circle so the map
        renders something; `zone_contains` is not."""
        self.user.zone_type = User.ZoneType.CITY
        self.user.zone_region = None
        self.assertEqual(self.user.effective_zone_mode, 'radius')
        with self.assertRaises(zones.ZoneBoundaryUnavailable):
            self.user.zone_contains(0.1, 0.1)


class LegacyZoneMigrationTests(TestCase):
    """What the migration promises, asserted against the post-migration model.

    The data migration itself runs in `users/migrations/0026_zone_type`; these
    check the shape it migrates *into* still behaves, which is the part a future
    change could break without the migration ever being re-run.
    """

    def test_a_default_zone_is_a_circle(self):
        """Every account that predates the field, and every new one."""
        user = make_user('fresh', latitude='10.0', longitude='10.0')
        self.assertEqual(user.zone_type, User.ZoneType.CIRCLE)
        self.assertEqual(user.zone_radius, 500)
        self.assertTrue(user.zone_contains(10.0027, 10.0))

    def test_a_legacy_circle_keeps_a_non_default_radius(self):
        user = make_user(
            'legacy', latitude='10.0', longitude='10.0', zone_radius=1500,
        )
        self.assertTrue(user.zone_contains(10.012, 10.0))    # ~1.3km
        self.assertFalse(user.zone_contains(10.018, 10.0))   # ~2km

    def test_boundary_availability_is_reportable_without_raising(self):
        """A sweep over many rows needs to skip broken zones, not catch per row."""
        broken = make_user(
            'broken', latitude='0.1', longitude='0.1', zone_type=User.ZoneType.CITY,
        )
        ok = make_user('ok', latitude='10.0', longitude='10.0')
        self.assertFalse(broken.zone_boundary_available)
        self.assertTrue(ok.zone_boundary_available)
