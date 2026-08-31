"""Antimeridian-safe geometry.

Rings here are written the way Nominatim sends them: wrapped, so a shape
crossing the dateline jumps from +179 to -179 between adjacent points. Every
fixture goes through the real unwrapping path, because that jump is the bug.
"""
import math
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from friends.models import FriendRequest
from users import visibility

from . import geometry, nominatim
from .tests import SQUARE, make_region, make_user, upstream_says

# Fiji: the main group straddles the meridian, 178E to 179W.
FIJI = [[178.0, -18.5], [-179.0, -18.5], [-179.0, -17.0], [178.0, -17.0], [178.0, -18.5]]
# Fiji as OSM actually holds it — two landmasses either side of the line, which
# is a MultiPolygon and the case where per-ring unwrapping is not enough.
FIJI_SPLIT = [
    [[178.0, -18.5], [179.9, -18.5], [179.9, -17.0], [178.0, -17.0], [178.0, -18.5]],
    [[-179.9, -18.5], [-179.0, -18.5], [-179.0, -17.0], [-179.9, -17.0], [-179.9, -18.5]],
]
# Kiribati: crosses the meridian just north of the equator.
KIRIBATI = [[179.0, 1.0], [-177.0, 1.0], [-177.0, 3.0], [179.0, 3.0], [179.0, 1.0]]
# Chukotka: the far north-eastern tip of Russia, crossing into the west.
CHUKOTKA = [[179.0, 64.0], [-176.0, 64.0], [-176.0, 67.0], [179.0, 67.0], [179.0, 64.0]]
# The Chatham Islands sit at 176.5 *west* — close to the meridian but entirely
# one side of it. Included precisely because they must NOT be treated as
# crossing: this is the fixture that proves existing regions do not move.
CHATHAM = [[-177.0, -44.4], [-175.8, -44.4], [-175.8, -43.6], [-177.0, -43.6], [-177.0, -44.4]]


class UnwrapTests(SimpleTestCase):
    def test_a_crossing_ring_becomes_continuous(self):
        [ring] = geometry.unwrap_rings([FIJI])
        lons = [lon for lon, _ in ring]
        # -179 is expressed as 181: three degrees from 178, not 357.
        self.assertEqual(max(lons), 181.0)
        self.assertEqual(min(lons), 178.0)

    def test_a_crossing_ring_gets_a_narrow_box_not_the_planet(self):
        rings = geometry.unwrap_rings([FIJI])
        _, min_lon, _, max_lon = geometry.bounds(rings)
        self.assertAlmostEqual(max_lon - min_lon, 3.0)
        self.assertTrue(geometry.crosses_antimeridian(min_lon, max_lon))

    def test_multipolygon_islands_share_one_window(self):
        """Per-ring unwrapping is not enough. Two landmasses either side of the
        line each unwrap correctly on their own and still land in windows 360
        apart, which puts the bounding box back around the whole planet."""
        rings = geometry.unwrap_rings(FIJI_SPLIT)
        _, min_lon, _, max_lon = geometry.bounds(rings)
        self.assertLess(max_lon - min_lon, 5.0)

    def test_a_nearby_but_non_crossing_ring_is_untouched(self):
        """The Chathams are 3.5 degrees from the meridian and must come through
        unchanged — the guarantee that no region already stored moves."""
        [ring] = geometry.unwrap_rings([CHATHAM])
        self.assertEqual(ring, [[float(lon), float(lat)] for lon, lat in CHATHAM])
        _, min_lon, _, max_lon = geometry.bounds([ring])
        self.assertFalse(geometry.crosses_antimeridian(min_lon, max_lon))

    def test_an_ordinary_ring_is_untouched(self):
        [ring] = geometry.unwrap_rings([SQUARE])
        self.assertEqual(ring, [[float(lon), float(lat)] for lon, lat in SQUARE])

    def test_centroid_comes_back_inside_the_ordinary_range(self):
        """The bounds stay unwrapped; the centroid cannot, because it is only
        ever handed to a map and a map wants a real longitude."""
        rings = geometry.unwrap_rings([KIRIBATI])
        _, centroid_lon = geometry.centroid(*geometry.bounds(rings))
        self.assertGreaterEqual(centroid_lon, -180.0)
        self.assertLessEqual(centroid_lon, 180.0)

    def test_align_lon_leaves_ordinary_windows_alone(self):
        self.assertEqual(geometry.align_lon(76.9, 76.7, 77.1), 76.9)
        self.assertEqual(geometry.align_lon(0.5, 0.0, 1.0), 0.5)

    def test_align_lon_moves_a_query_into_a_crossing_window(self):
        self.assertAlmostEqual(geometry.align_lon(-179.9, 178.0, 181.0), 180.1)


class AntimeridianContainsTests(TestCase):
    """Points either side of the line, for each place that has to work."""

    def test_fiji(self):
        region = make_region(rings=[FIJI], osm_id=2001, name='Fiji')
        self.assertTrue(region.crosses_antimeridian)
        # Immediately either side of the line, both plainly inside the country.
        self.assertTrue(region.contains(-17.5, 179.99))
        self.assertTrue(region.contains(-17.5, -179.99))
        self.assertTrue(region.contains(-17.5, 180.0))
        self.assertTrue(region.contains(-17.5, -180.0))
        # Well inside, from either end.
        self.assertTrue(region.contains(-17.5, 178.5))
        self.assertTrue(region.contains(-17.5, -179.5))
        # Clearly outside: west of the region, and east past its far edge.
        self.assertFalse(region.contains(-17.5, 177.0))
        self.assertFalse(region.contains(-17.5, -178.0))
        # Outside by latitude, inside by longitude.
        self.assertFalse(region.contains(-20.0, 179.5))

    def test_fiji_as_two_islands(self):
        region = make_region(rings=FIJI_SPLIT, osm_id=2002, name='Fiji')
        self.assertTrue(region.contains(-17.5, 179.0))    # eastern island
        self.assertTrue(region.contains(-17.5, -179.5))   # western island
        self.assertFalse(region.contains(-17.5, 179.95))  # the strait between

    def test_kiribati(self):
        region = make_region(rings=[KIRIBATI], osm_id=2003, name='Kiribati')
        self.assertTrue(region.contains(2.0, 179.9))
        self.assertTrue(region.contains(2.0, -179.9))
        self.assertTrue(region.contains(2.0, -177.5))
        self.assertFalse(region.contains(2.0, 178.0))
        self.assertFalse(region.contains(2.0, -176.0))
        self.assertFalse(region.contains(5.0, 179.9))

    def test_chukotka(self):
        region = make_region(rings=[CHUKOTKA], osm_id=2004, name='Chukotka')
        self.assertTrue(region.contains(65.5, 179.5))
        self.assertTrue(region.contains(65.5, -179.5))
        self.assertTrue(region.contains(65.5, -177.0))
        self.assertFalse(region.contains(65.5, 178.0))
        self.assertFalse(region.contains(65.5, -175.0))
        self.assertFalse(region.contains(60.0, -179.5))

    def test_chatham_islands_do_not_cross_and_still_work(self):
        region = make_region(rings=[CHATHAM], osm_id=2005, name='Chatham Islands')
        self.assertFalse(region.crosses_antimeridian)
        self.assertTrue(region.contains(-44.0, -176.4))
        self.assertFalse(region.contains(-44.0, 179.0))
        self.assertFalse(region.contains(-44.0, -179.0))

    def test_an_ordinary_region_is_unaffected_by_any_of_this(self):
        region = make_region(osm_id=2006)
        self.assertFalse(region.crosses_antimeridian)
        self.assertTrue(region.contains(0.5, 0.5))
        self.assertFalse(region.contains(0.5, 50.0))
        # Nothing near the meridian can reach a region that is nowhere near it.
        self.assertFalse(region.contains(0.5, 179.5))
        self.assertFalse(region.contains(0.5, -179.5))

    def test_longitudes_a_whole_turn_apart_are_the_same_meridian(self):
        """Not a quirk of the alignment — -359.5 and 0.5 are one place. The API
        validates its input to ±180 long before this, so nothing arrives here
        that way in practice; it is asserted because the alignment step has to
        be a no-op on meaning, not just on numbers."""
        region = make_region(osm_id=2007)
        self.assertTrue(region.contains(0.5, 0.5))
        self.assertTrue(region.contains(0.5, -359.5))
        self.assertTrue(region.contains(0.5, 360.5))


class AntimeridianBboxTests(TestCase):
    def test_upstream_planet_wide_box_is_refused_for_a_crossing_region(self):
        """Nominatim reports a crossing region's box wrapped, as -180 to 180 —
        a box around the entire planet, which rejects nothing. For those the
        geometry is the only honest source."""
        rings = geometry.unwrap_rings([FIJI])
        _, min_lon, _, max_lon = nominatim._bbox(
            {'boundingbox': ['-18.5', '-17.0', '-180', '180']}, rings,
        )
        self.assertEqual((min_lon, max_lon), (178.0, 181.0))

    def test_upstream_box_is_still_preferred_for_everything_else(self):
        _, min_lon, _, max_lon = nominatim._bbox(
            {'boundingbox': ['43.1', '43.4', '76.7', '77.1']}, [SQUARE],
        )
        self.assertEqual((min_lon, max_lon), (76.7, 77.1))

    def test_a_crossing_payload_is_stored_measurable(self):
        """End to end through the real upsert: wrapped geometry in, a window
        that can actually answer a containment question out."""
        region = nominatim._region_from_payload({
            'osm_type': 'relation', 'osm_id': 2100, 'name': 'Fiji',
            'display_name': 'Fiji', 'addresstype': 'city', 'category': 'boundary',
            'boundingbox': ['-18.5', '-17.0', '-180', '180'],
            'geojson': {'type': 'Polygon', 'coordinates': [FIJI]},
        })
        self.assertIsNotNone(region)
        self.assertTrue(region.crosses_antimeridian)
        self.assertTrue(region.contains(-17.5, -179.5))
        self.assertTrue(region.contains(-17.5, 178.5))


class AntimeridianForPointTests(TestCase):
    """The SQL prefilter has to find a crossing region too.

    It compares a ±180 longitude against a window that runs past 180, so
    without the shifted clauses the row is filtered out in the database and the
    cache never gets a chance to answer — every lookup falls through to
    Nominatim and, being a name search, mostly fails.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(make_user('me'))
        self.fiji = make_region(rings=[FIJI], osm_id=2200, name='Fiji', kind='city')

    def get(self, lat, lon):
        return self.client.get(f'/api/regions/for-point/?lat={lat}&lon={lon}')

    def test_a_point_east_of_the_line_finds_it(self):
        with patch('regions.nominatim.address_at') as address:
            body = self.get(-17.5, 179.5).json()
        self.assertEqual(body['id'], self.fiji.pk)
        address.assert_not_called()

    def test_a_point_west_of_the_line_finds_it(self):
        with patch('regions.nominatim.address_at') as address:
            body = self.get(-17.5, -179.5).json()
        self.assertEqual(body['id'], self.fiji.pk)
        address.assert_not_called()

    def test_a_point_outside_still_misses(self):
        with upstream_says(address=None):
            self.assertEqual(self.get(-17.5, 100.0).status_code, 404)

    def test_an_ordinary_lookup_is_unchanged(self):
        make_region(osm_id=2201, name='Square', kind='city')
        with patch('regions.nominatim.address_at') as address:
            body = self.get(0.5, 0.5).json()
        self.assertEqual(body['name'], 'Square')
        address.assert_not_called()


# ─── Imprecise positions ──────────────────────────────────────────────────────
class ToleranceTests(TestCase):
    """Containment when the position being tested is one we fuzzed ourselves.

    An Approximate sharer's coordinates are snapped to a ~500m grid before
    storage, so the point we hold can be a few hundred metres from where they
    are. City district borders run down streets. Tested strictly, that told
    people standing in Бостандық that they were not in Бостандық.
    """

    def setUp(self):
        # A degree of longitude is ~111km at the equator, so this square is
        # roughly 111km on a side and its edge is a long way from its middle.
        self.region = make_region(osm_id=2400, name='Bostandyq', rings=[SQUARE])

    def test_no_tolerance_is_exactly_the_strict_test(self):
        self.assertTrue(self.region.could_contain(0.5, 0.5, 0))
        self.assertFalse(self.region.could_contain(1.5, 0.5, 0))

    def test_a_point_just_outside_is_accepted_within_tolerance(self):
        """~200m north of the border, with a 354m grid error to explain it."""
        just_outside = 1.0 + (200 / 111_320.0)
        self.assertFalse(self.region.contains(just_outside, 0.5))
        self.assertTrue(self.region.could_contain(just_outside, 0.5, 354))

    def test_a_point_well_outside_is_still_refused(self):
        """The rule this check exists for — you cannot blanket a city you are
        not in — has to survive the tolerance. A few hundred metres of slack
        must not become a few kilometres."""
        well_outside = 1.0 + (5000 / 111_320.0)
        self.assertFalse(self.region.could_contain(well_outside, 0.5, 354))

    def test_the_far_side_of_the_world_is_never_accepted(self):
        self.assertFalse(self.region.could_contain(50.0, 50.0, 354))

    def test_tolerance_works_across_the_antimeridian(self):
        fiji = make_region(rings=[FIJI], osm_id=2401, name='Fiji')
        just_outside = -18.5 - (200 / 111_320.0)
        self.assertFalse(fiji.contains(just_outside, 179.5))
        self.assertTrue(fiji.could_contain(just_outside, 179.5, 354))
        self.assertTrue(fiji.could_contain(just_outside, -179.5, 354))


class PositionToleranceTests(TestCase):
    """Which *viewers* get slack, and how much.

    The question moved. It used to be a property of the person being looked
    at — an Approximate sharer's stored point was a grid centre, so anything
    comparing it against a boundary had to allow for that. Nothing is fuzzed
    before storage now, so the slack belongs to the pair: a friend sees the
    real point and needs none, everybody else sees a cell and needs half its
    diagonal.
    """

    def test_a_friend_gets_none(self):
        target = make_user('precise')
        friend = make_user('their-friend')
        FriendRequest.objects.create(
            sender=friend, receiver=target, status=FriendRequest.Status.ACCEPTED,
        )
        self.assertEqual(visibility.position_tolerance_for(friend, target), 0.0)

    def test_looking_at_yourself_gets_none(self):
        user = make_user('self-view')
        self.assertEqual(visibility.position_tolerance_for(user, user), 0.0)

    def test_a_stranger_gets_half_a_cell_diagonal(self):
        target = make_user('fuzzy')
        stranger = make_user('a-stranger')
        tolerance = visibility.position_tolerance_for(stranger, target)
        # Enough to cover the worst case of a 500m grid snap, and not much more.
        self.assertGreater(tolerance, 350)
        self.assertLess(tolerance, 400)

    def test_the_tolerance_covers_the_worst_snap_it_has_to_explain(self):
        """The number is only defensible if it actually bounds the error the
        snapping introduces, so this measures one rather than trusting it."""
        target = make_user('measured')
        stranger = make_user('measuring-stranger')
        lat, lon = 43.2380, 76.9450          # Almaty, near a district border
        snapped_lat, snapped_lon = visibility.snap_coordinates(target, lat, lon)
        moved_m = math.hypot(
            (float(snapped_lat) - lat) * 111_320.0,
            (float(snapped_lon) - lon) * 111_320.0 * math.cos(math.radians(lat)),
        )
        self.assertLessEqual(
            moved_m, visibility.position_tolerance_for(stranger, target),
        )
