"""A city or district zone that has no boundary yet gets one.

New accounts start as a city zone, and the location report only reads stored
boundaries. Before `users.zone_repair`, the first people in a city Approx had
never seen were answered from the 500m fallback circle indefinitely, with
nothing saying so. These tests stand somebody in exactly that city.

The map service is stubbed the same way `test_zone_follow` stubs it, with one
addition: a fetch that is allowed upstream "finds" the city by storing its
boundary first, which is what the real upstream path does as a side effect.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from regions import places, upstream
from regions.models import Region
from users import zone_repair
from users.models import User
from users.test_zone_follow import make_region

LOCATION_URL = '/api/proximity/location/'
ZONE_URL = '/api/users/zone/'
RETRY_URL = '/api/users/zone/boundary/'
IN_ALMATY = {'latitude': 0.4, 'longitude': 0.4, 'accuracy': 10}

real_snapshot_for = places.snapshot_for


@override_settings(ZONE_BOUNDARY_REPAIR='inline')
class RepairTests(TestCase):
    def setUp(self):
        cache.clear()
        for target, value in (
            ('regions.nominatim.address_at', None),
            ('regions.nominatim._resolve_boundary', None),
            ('regions.overpass.boundaries_at', []),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.upstream_calls = 0
        self.upstream_fails = False
        patcher = patch('regions.places.snapshot_for', side_effect=self.snapshot_for)
        patcher.start()
        self.addCleanup(patcher.stop)

        # A brand-new account: city zone, no boundary, the model's defaults.
        self.user = User.objects.create_user(
            username='newcomer', email='newcomer@example.com', password='testpass-123',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def snapshot_for(self, user, allow_upstream=False, force=False):
        if allow_upstream:
            self.upstream_calls += 1
            if self.upstream_fails:
                raise upstream.UpstreamError('map service down')
            if not Region.objects.filter(name='Almaty').exists():
                make_region('Almaty', (0, 0, 1, 1), Region.Kind.CITY, 4)
        return real_snapshot_for(user, allow_upstream=False, force=force)

    def report(self):
        response = self.client.patch(LOCATION_URL, IN_ALMATY, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.user.refresh_from_db()

    def test_a_new_city_gets_its_boundary_from_the_location_report(self):
        self.assertTrue(zone_repair.needs_boundary(self.user))
        self.report()
        self.assertEqual(self.user.zone_region.name, 'Almaty')
        zone = self.client.get(ZONE_URL).json()
        self.assertFalse(zone['zone_boundary_missing'])
        self.assertEqual(zone['zone_place_name'], 'Almaty')
        self.assertEqual(zone['effective_zone_mode'], 'region')

    def test_a_failing_map_service_neither_breaks_the_report_nor_gets_hammered(self):
        self.upstream_fails = True
        for _ in range(5):
            self.report()
        self.assertIsNone(self.user.zone_region_id)
        self.assertEqual(self.upstream_calls, 1)
        self.assertTrue(self.client.get(ZONE_URL).json()['zone_boundary_missing'])

        # Once the cooldown has passed, the next report tries again.
        cache.delete(f'zone-repair:{self.user.pk}')
        self.upstream_fails = False
        self.report()
        self.assertEqual(self.user.zone_region.name, 'Almaty')

    def test_retry_fetches_now_and_says_whether_it_landed(self):
        self.upstream_fails = True
        self.report()
        self.upstream_fails = False
        response = self.client.post(RETRY_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['zone_boundary_missing'])
        self.assertEqual(response.json()['zone_place_name'], 'Almaty')

    def test_retry_is_limited_while_the_service_keeps_failing(self):
        self.upstream_fails = True
        self.report()
        first = self.client.post(RETRY_URL)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['zone_boundary_missing'])
        self.assertEqual(self.client.post(RETRY_URL).status_code, 429)

    def test_a_zone_that_has_its_boundary_is_left_alone(self):
        self.report()
        calls = self.upstream_calls
        cache.clear()
        self.report()
        self.client.post(RETRY_URL)
        self.assertEqual(self.upstream_calls, calls)

    def test_a_circle_zone_never_fetches(self):
        self.user.zone_type = User.ZoneType.CIRCLE
        self.user.save(update_fields=['zone_type'])
        self.report()
        self.assertEqual(self.upstream_calls, 0)


class BackgroundTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(ZONE_BOUNDARY_REPAIR='background')
    def test_the_report_hands_the_fetch_off_instead_of_waiting(self):
        user = User.objects.create_user(username='a', email='a@example.com', password='testpass-123')
        client = APIClient()
        client.force_authenticate(user)
        with patch.object(zone_repair._executor, 'submit') as submit, \
                patch('users.zone_repair.repair') as repair:
            response = client.patch(LOCATION_URL, IN_ALMATY, format='json')
        self.assertEqual(response.status_code, 200)
        submit.assert_called_once_with(zone_repair._repair_in_background, user.pk)
        repair.assert_not_called()


class FallbackLogTests(TestCase):
    def test_the_fallback_is_logged_with_the_zone_type_and_no_position(self):
        user = User.objects.create_user(username='b', email='b@example.com', password='testpass-123')
        client = APIClient()
        client.force_authenticate(user)
        with self.assertLogs('proximity.views', level='WARNING') as logs:
            client.post('/api/map/nearby/query/', {'latitude': 0.4, 'longitude': 0.4}, format='json')
        records = [r for r in logs.records if r.getMessage() == 'zone_boundary_fallback']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].zone_type, User.ZoneType.CITY)
        self.assertFalse(any(hasattr(records[0], f) for f in ('latitude', 'longitude', 'lat', 'lon')))
