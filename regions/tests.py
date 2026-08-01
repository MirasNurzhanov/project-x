"""Geometry and caching behaviour for region-shaped zones.

Nothing here touches the network: `nominatim._get` is the single seam every
upstream call passes through, so patching it exercises the whole parse →
simplify → cache path against fixed payloads.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from . import nominatim
from .models import Region

User = get_user_model()


def make_user(username='u'):
    return User.objects.create_user(
        username=username, email=f'{username}@e.com', password='pw',
    )


# A unit square from (0,0) to (1,1), as [lon, lat] like GeoJSON.
SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
# The same square with a hole from (0.4,0.4) to (0.6,0.6).
HOLE = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]


def make_region(rings=None, **kwargs):
    rings = rings if rings is not None else [SQUARE]
    lats = [p[1] for r in rings for p in r]
    lons = [p[0] for r in rings for p in r]
    defaults = dict(
        osm_type='relation', osm_id=1, name='Square', display_name='Square, Testland',
        kind='city',
        min_lat=min(lats), min_lon=min(lons), max_lat=max(lats), max_lon=max(lons),
        centroid_lat=sum(lats) / len(lats), centroid_lon=sum(lons) / len(lons),
        rings=rings, point_count=sum(len(r) for r in rings),
    )
    defaults.update(kwargs)
    return Region.objects.create(**defaults)


# ─── Containment ──────────────────────────────────────────────────────────────
class ContainsTests(TestCase):
    def setUp(self):
        self.region = make_region()

    def test_point_inside(self):
        self.assertTrue(self.region.contains(0.5, 0.5))

    def test_point_outside_the_ring_but_inside_the_bbox_is_out(self):
        """A concave shape is the case a bbox test alone gets wrong."""
        # An L: the notch at (0.75, 0.75) sits inside the bounding box but
        # outside the polygon.
        el = [[0, 0], [1, 0], [1, 0.5], [0.5, 0.5], [0.5, 1], [0, 1], [0, 0]]
        region = make_region(rings=[el], osm_id=2)
        self.assertTrue(region.contains(0.25, 0.25))
        self.assertFalse(region.contains(0.75, 0.75))

    def test_point_far_outside(self):
        self.assertFalse(self.region.contains(50.0, 50.0))

    def test_hole_is_not_inside(self):
        region = make_region(rings=[SQUARE, HOLE], osm_id=3)
        self.assertTrue(region.contains(0.2, 0.2))   # in the square
        self.assertFalse(region.contains(0.5, 0.5))  # in the hole

    def test_ray_through_a_vertex_is_not_double_counted(self):
        """A ray crossing exactly at a shared vertex must count once, or a
        point plainly inside reads as outside."""
        diamond = [[0.0, 0.5], [0.5, 0.0], [1.0, 0.5], [0.5, 1.0], [0.0, 0.5]]
        region = make_region(rings=[diamond], osm_id=4)
        # y=0.5 runs straight through the left and right vertices.
        self.assertTrue(region.contains(0.5, 0.5))

    def test_degenerate_ring_contains_nothing(self):
        region = make_region(rings=[[[0, 0], [1, 1]]], osm_id=5)
        self.assertFalse(region.contains(0.5, 0.5))


# ─── Simplification ───────────────────────────────────────────────────────────
class SimplifyTests(TestCase):
    def test_collinear_points_collapse(self):
        line = [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0]]
        self.assertEqual(nominatim.simplify(line, 0.0001), [[0, 0], [4, 0]])

    def test_corner_survives(self):
        shape = [[0, 0], [1, 0], [2, 0], [2, 2]]
        result = nominatim.simplify(shape, 0.0001)
        self.assertIn([2, 0], result)

    def test_short_input_is_returned_intact(self):
        self.assertEqual(nominatim.simplify([[0, 0], [1, 1]], 0.1), [[0, 0], [1, 1]])

    def test_deep_input_does_not_blow_the_stack(self):
        """The recursive form of this algorithm overflows on a real coastline;
        the iterative one must not."""
        import sys
        spiral = [[i * 0.001, (i % 2) * 0.01] for i in range(sys.getrecursionlimit() * 3)]
        result = nominatim.simplify(spiral, 0.0001)
        self.assertGreater(len(result), 2)

    def test_ring_set_is_capped(self):
        """Tolerance backs off until the whole set fits, so one huge coastline
        cannot produce a payload that stalls the map."""
        big = [[i * 0.0001, (i % 3) * 0.0001] for i in range(20000)]
        rings, total = nominatim._simplify_rings([big])
        self.assertLessEqual(total, nominatim.MAX_POINTS)
        self.assertEqual(total, sum(len(r) for r in rings))


# ─── Payload parsing ──────────────────────────────────────────────────────────
class PayloadTests(TestCase):
    def test_multipolygon_flattens_to_rings(self):
        rings = nominatim._rings_from_geojson({
            'type': 'MultiPolygon',
            'coordinates': [[SQUARE], [HOLE]],
        })
        self.assertEqual(len(rings), 2)

    def test_point_geometry_yields_no_rings(self):
        rings = nominatim._rings_from_geojson({'type': 'Point', 'coordinates': [1, 2]})
        self.assertEqual(rings, [])

    def test_bounding_box_order_is_lat_lat_lon_lon(self):
        """Nominatim interleaves its bbox differently to everything else; get
        this wrong and every containment test silently fails."""
        min_lat, min_lon, max_lat, max_lon = nominatim._bbox(
            {'boundingbox': ['43.1', '43.4', '76.7', '77.1']}, [SQUARE],
        )
        self.assertEqual((min_lat, max_lat), (43.1, 43.4))
        self.assertEqual((min_lon, max_lon), (76.7, 77.1))

    def test_place_without_a_boundary_is_rejected(self):
        """A village that OSM only knows as a pin cannot bound a zone."""
        region = nominatim._region_from_payload({
            'osm_type': 'node', 'osm_id': 99, 'name': 'Pin',
            'display_name': 'Pin', 'geojson': {'type': 'Point', 'coordinates': [1, 2]},
        })
        self.assertIsNone(region)

    def test_known_osm_object_is_not_refetched(self):
        existing = make_region(osm_id=555)
        again = nominatim._region_from_payload({
            'osm_type': 'relation', 'osm_id': 555, 'name': 'Square',
            'display_name': 'Square', 'geojson': {'type': 'Polygon', 'coordinates': [SQUARE]},
        })
        self.assertEqual(again.pk, existing.pk)
        self.assertEqual(Region.objects.count(), 1)


# ─── Rate limiting ────────────────────────────────────────────────────────────
class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_first_caller_takes_the_slot_immediately(self):
        with patch('regions.nominatim.time.sleep') as sleep:
            nominatim._throttle()
        sleep.assert_not_called()

    def test_second_caller_waits_instead_of_proceeding(self):
        """Nominatim blocks applications that exceed 1 req/s, so a second
        caller yielding rather than firing is a correctness property."""
        nominatim._throttle()  # claims the slot
        # Slot is held, then frees on the retry.
        with patch('regions.nominatim.cache.add', side_effect=[False, True]), \
             patch('regions.nominatim.time.sleep') as sleep:
            nominatim._throttle()
        sleep.assert_called_once()

    def test_unreachable_cache_degrades_instead_of_500ing(self):
        """Redis restarting must not turn a place search into a server error."""
        nominatim._local_next_allowed = 0.0
        with patch('regions.nominatim.cache.add', side_effect=ConnectionError('redis down')):
            nominatim._throttle()          # first call still gets through
            self.assertFalse(nominatim._claim_slot())  # second is still held back

    def test_a_slot_that_never_frees_raises_rather_than_hanging(self):
        """A wedged throttle must surface as an error the view can turn into a
        503 — never as a request that hangs until the proxy kills it."""
        with patch('regions.nominatim.cache.add', return_value=False), \
             patch('regions.nominatim.time.sleep'), \
             patch('regions.nominatim.time.monotonic', side_effect=[0, 1, 999]):
            with self.assertRaises(nominatim.NominatimError):
                nominatim._throttle()


# ─── Endpoints ────────────────────────────────────────────────────────────────
class RegionEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user('me')
        self.client.force_authenticate(self.user)

    def test_search_requires_authentication(self):
        self.assertEqual(APIClient().get('/api/regions/search/?q=almaty').status_code, 401)

    def test_short_query_never_reaches_upstream(self):
        with patch('regions.nominatim._get') as get:
            body = self.client.get('/api/regions/search/?q=a').json()
        self.assertEqual(body, [])
        get.assert_not_called()

    def test_cached_hits_answer_without_upstream(self):
        for i in range(5):
            make_region(osm_id=100 + i, name=f'Almaty {i}', display_name=f'Almaty {i}, KZ')
        with patch('regions.nominatim._get') as get:
            body = self.client.get('/api/regions/search/?q=almaty').json()
        self.assertEqual(len(body), 5)
        get.assert_not_called()

    def test_search_result_omits_the_geometry(self):
        """The rings are ~100x the rest of the payload and a picker never draws
        them."""
        make_region(osm_id=200, name='Almaty', display_name='Almaty, KZ')
        with patch('regions.nominatim._get', return_value=[]):
            body = self.client.get('/api/regions/search/?q=almaty').json()
        self.assertNotIn('rings', body[0])
        self.assertIn('min_lat', body[0])

    def test_detail_carries_the_geometry(self):
        region = make_region(osm_id=300)
        body = self.client.get(f'/api/regions/{region.pk}/').json()
        self.assertEqual(body['rings'], [SQUARE])

    def test_upstream_failure_falls_back_to_cached_results(self):
        make_region(osm_id=400, name='Almaty', display_name='Almaty, KZ')
        with patch('regions.nominatim.search', side_effect=nominatim.NominatimError('down')):
            response = self.client.get('/api/regions/search/?q=almaty')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_upstream_failure_with_nothing_cached_is_a_503(self):
        with patch('regions.nominatim.search', side_effect=nominatim.NominatimError('down')):
            response = self.client.get('/api/regions/search/?q=nowhere')
        self.assertEqual(response.status_code, 503)

    def test_for_point_prefers_the_smallest_containing_region(self):
        """A point in a city is also in its province; "my city" is the tighter."""
        make_region(osm_id=500, name='Province', rings=[
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        ])
        city = make_region(osm_id=501, name='City', rings=[SQUARE])
        with patch('regions.nominatim.reverse') as reverse:
            body = self.client.get('/api/regions/for-point/?lat=0.5&lon=0.5').json()
        self.assertEqual(body['id'], city.pk)
        reverse.assert_not_called()

    def test_for_point_rejects_out_of_range_coordinates(self):
        response = self.client.get('/api/regions/for-point/?lat=91&lon=0')
        self.assertEqual(response.status_code, 400)

    def test_for_point_without_coordinates_is_a_400(self):
        self.assertEqual(self.client.get('/api/regions/for-point/').status_code, 400)

    def test_for_point_with_no_mapped_area_is_a_404(self):
        miss = nominatim.ReverseResult(None, None, None)
        with patch('regions.nominatim.reverse', return_value=miss):
            response = self.client.get('/api/regions/for-point/?lat=0.5&lon=0.5')
        self.assertEqual(response.status_code, 404)


# ─── Zone settings ────────────────────────────────────────────────────────────
class ZoneShapeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user('me')
        self.client.force_authenticate(self.user)

    def patch_zone(self, **payload):
        return self.client.patch('/api/users/zone/', payload, format='json')

    def test_radius_is_now_a_free_dial(self):
        """The old field was a 100/500/1000 choice; anything in range must
        stick now."""
        self.assertEqual(self.patch_zone(zone_radius=1337).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_radius, 1337)

    def test_radius_below_the_floor_is_rejected(self):
        self.assertEqual(self.patch_zone(zone_radius=10).status_code, 400)

    def test_radius_above_the_ceiling_is_rejected(self):
        response = self.patch_zone(zone_radius=50000)
        self.assertEqual(response.status_code, 400)
        self.assertIn('region', str(response.json()).lower())

    def test_region_mode_needs_a_region(self):
        response = self.patch_zone(zone_mode='region')
        self.assertEqual(response.status_code, 400)
        self.assertIn('zone_region', response.json())

    def test_region_mode_with_a_region_sticks(self):
        region = make_region(osm_id=600)
        response = self.patch_zone(zone_mode='region', zone_region=region.pk)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, region.pk)
        self.assertEqual(self.user.effective_zone_mode, 'region')

    def test_switching_modes_keeps_both_shapes(self):
        """Going back to a circle must not forget the city, and vice versa."""
        region = make_region(osm_id=601)
        self.patch_zone(zone_radius=2500)
        self.patch_zone(zone_mode='region', zone_region=region.pk)
        self.patch_zone(zone_mode='radius')
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_radius, 2500)
        self.assertEqual(self.user.zone_region_id, region.pk)

    def test_evicted_region_falls_back_to_the_circle(self):
        """Regions are a shared cache. Deleting one must not delete the user,
        nor leave the map trying to draw a polygon it can never fetch."""
        region = make_region(osm_id=602)
        self.patch_zone(zone_mode='region', zone_region=region.pk)
        region.delete()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.zone_region_id)
        self.assertEqual(self.user.effective_zone_mode, 'radius')

    def test_nearby_advertises_the_mode_and_region_id_only(self):
        """The map polls this every 10s — the outline must not ride along."""
        region = make_region(osm_id=603)
        self.patch_zone(zone_mode='region', zone_region=region.pk, zone_active=True)

        other = make_user('other')
        other.latitude, other.longitude = 0.5, 0.5
        other.save()
        self.user.latitude, self.user.longitude = 0.5, 0.5
        self.user.save()

        self.client.force_authenticate(other)
        body = self.client.get('/api/proximity/nearby/').json()
        row = next((u for u in body if u['username'] == 'me'), None)
        self.assertIsNotNone(row)
        self.assertEqual(row['zone_mode'], 'region')
        self.assertEqual(row['zone_region_id'], region.pk)
        self.assertNotIn('rings', row)

# ─── Place size ───────────────────────────────────────────────────────────────
class ClassifyTests(TestCase):
    def test_addresstype_wins(self):
        for addresstype, expected in (
            ('city', 'city'), ('town', 'city'), ('village', 'city'),
            ('suburb', 'district'), ('neighbourhood', 'district'),
            ('city_district', 'district'),
        ):
            self.assertEqual(
                nominatim._classify({'addresstype': addresstype}), expected, addresstype,
            )

    def test_admin_level_breaks_the_tie_when_the_word_is_ambiguous(self):
        """'borough' is a whole city in one country and a slice of one in
        another, so the word alone cannot decide."""
        self.assertEqual(
            nominatim._classify({'extratags': {'admin_level': '8'}}), 'city',
        )
        self.assertEqual(
            nominatim._classify({'extratags': {'admin_level': '10'}}), 'district',
        )

    def test_a_country_is_neither_size_on_offer(self):
        self.assertEqual(
            nominatim._classify({'addresstype': 'country', 'extratags': {'admin_level': '2'}}),
            'other',
        )

    def test_unknown_place_with_no_level_is_other(self):
        self.assertEqual(nominatim._classify({}), 'other')


class ForPointLevelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user('me')
        self.client.force_authenticate(self.user)
        # A district nested inside a city, both containing (0.5, 0.5).
        self.city = make_region(
            osm_id=900, name='Metropolis', kind='city',
            rings=[[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        )
        self.district = make_region(
            osm_id=901, name='Old Town', kind='district', rings=[SQUARE],
        )

    def get(self, level=None):
        url = '/api/regions/for-point/?lat=0.5&lon=0.5'
        if level:
            url += f'&level={level}'
        return self.client.get(url)

    def test_city_lookup_does_not_return_the_district(self):
        """The district is the smaller shape, so an unfiltered "smallest
        containing" search would wrongly prefer it."""
        with patch('regions.nominatim.reverse') as reverse:
            body = self.get('city').json()
        self.assertEqual(body['id'], self.city.pk)
        reverse.assert_not_called()

    def test_district_lookup_returns_the_district(self):
        with patch('regions.nominatim.reverse') as reverse:
            body = self.get('district').json()
        self.assertEqual(body['id'], self.district.pk)
        reverse.assert_not_called()

    def test_level_defaults_to_city(self):
        with patch('regions.nominatim.reverse'):
            self.assertEqual(self.get().json()['id'], self.city.pk)

    def test_unknown_level_is_rejected(self):
        self.assertEqual(self.get('planet').status_code, 400)

    def test_miss_names_the_city_it_searched(self):
        """A bare 404 cannot tell the user which city has no districts mapped,
        and cannot point them at the other button."""
        Region.objects.all().delete()
        miss = nominatim.ReverseResult(None, None, 'Almaty')
        with patch('regions.nominatim.reverse', return_value=miss):
            response = self.get('district')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['city_name'], 'Almaty')
        self.assertIn('Almaty', response.json()['detail'])


# ─── Level resolution ─────────────────────────────────────────────────────────
class ReverseByAddressTests(TestCase):
    """Choosing a level by reverse *zoom* is what broke in the field: in Almaty
    zoom 10 returns a district and zoom 14 returns a node with no boundary at
    all. These pin the address-hierarchy approach that replaced it."""

    ALMATY_ADDRESS = {
        'address': {
            'suburb': 'Tastak',
            'city_district': 'Almaly',
            'city': 'Almaty',
            'country_code': 'kz',
        },
    }

    def setUp(self):
        cache.clear()

    def city_polygon(self, name, ring, addresstype, osm_id):
        return {
            'osm_type': 'relation', 'osm_id': osm_id, 'name': name,
            'display_name': f'{name}, Kazakhstan', 'addresstype': addresstype,
            'category': 'boundary',
            'geojson': {'type': 'Polygon', 'coordinates': [ring]},
            'address': {'country_code': 'kz'},
        }

    def test_city_lookup_returns_the_city_not_the_district(self):
        """The bug as reported: the city button showed a district."""
        big = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
        responses = [self.ALMATY_ADDRESS, [self.city_polygon('Almaty', big, 'city', 11)]]
        with patch('regions.nominatim._get', side_effect=responses):
            result = nominatim.reverse(0.5, 0.5, kind='city')
        self.assertIsNotNone(result.region)
        self.assertEqual(result.region.name, 'Almaty')
        self.assertEqual(result.region.kind, 'city')

    def test_district_lookup_finds_a_boundary_where_zoom_found_a_bare_node(self):
        """The other half: the district button returned nothing, because the
        fine zoom resolved to a place node with no polygon."""
        responses = [
            self.ALMATY_ADDRESS,
            [self.city_polygon('Almaly', SQUARE, 'city_district', 12)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = nominatim.reverse(0.5, 0.5, kind='district')
        self.assertIsNotNone(result.region)
        self.assertEqual(result.region.kind, 'district')

    def test_district_query_is_qualified_by_its_city(self):
        """"Almaly" alone is ambiguous; "Almaly, Almaty" is not."""
        with patch('regions.nominatim._get', side_effect=[self.ALMATY_ADDRESS, []]) as get:
            nominatim.reverse(0.5, 0.5, kind='district')
        self.assertEqual(get.call_args.args[1]['q'], 'Almaly, Almaty')
        self.assertEqual(get.call_args.args[1]['countrycodes'], 'kz')

    def test_a_same_named_place_elsewhere_is_rejected(self):
        """Name search is only safe because the result has to contain the
        point we started from — there are many places called Almaty."""
        far_away = [[50, 50], [51, 50], [51, 51], [50, 51], [50, 50]]
        responses = [
            self.ALMATY_ADDRESS,
            [self.city_polygon('Almaty', far_away, 'city', 13)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = nominatim.reverse(0.5, 0.5, kind='city')
        self.assertIsNone(result.region)
        # Still cached for anyone who really is there.
        self.assertTrue(Region.objects.filter(osm_id=13).exists())

    def test_missing_level_reports_the_city_without_a_second_call(self):
        address = {'address': {'city': 'Flatville', 'country_code': 'kz'}}
        with patch('regions.nominatim._get', side_effect=[address]) as get:
            result = nominatim.reverse(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assertEqual(result.city_name, 'Flatville')
        self.assertEqual(get.call_count, 1)

    def test_reverse_reads_the_hierarchy_at_one_fixed_zoom(self):
        with patch('regions.nominatim._get', side_effect=[self.ALMATY_ADDRESS, []]) as get:
            nominatim.reverse(0.5, 0.5, kind='city')
        self.assertEqual(get.call_args_list[0].args[1]['zoom'], nominatim.ADDRESS_ZOOM)
        self.assertEqual(get.call_args_list[0].args[1]['addressdetails'], 1)
