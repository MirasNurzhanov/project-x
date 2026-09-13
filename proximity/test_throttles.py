from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from users.models import User


class MapThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username='mapuser', email='map@example.test')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_all_read_routes_share_the_same_user_budget(self):
        routes = [('post', '/api/map/nearby/query/'), ('post', '/api/proximity/nearby/query/'),
                  ('get', '/api/map/nearby/'), ('get', '/api/proximity/nearby/')]
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'map_query': '2/min'}):
            # Invalid anchors still consume the limit; validation cannot be used
            # to avoid the throttle, and changing endpoints cannot reset it.
            self.assertEqual(self.client.post(routes[0][1], {}).status_code, 400)
            self.assertEqual(self.client.post(routes[1][1], {}).status_code, 400)
            for method, path in routes:
                res = getattr(self.client, method)(path, {})
                self.assertEqual(res.status_code, 429)
                self.assertIn('Retry-After', res)
            other = User.objects.create_user(username='othermap', email='othermap@example.test')
            self.client.force_authenticate(other)
            self.assertNotEqual(self.client.post(routes[0][1], {}).status_code, 429)

    def test_default_budget_allows_two_polls_every_ten_seconds_for_an_hour(self):
        with patch.object(ScopedRateThrottle, 'timer', return_value=1000):
            for _ in range(720):
                self.assertEqual(self.client.post('/api/map/nearby/query/', {}).status_code, 400)
