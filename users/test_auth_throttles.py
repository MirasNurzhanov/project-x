from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from users.models import User


class AuthenticationThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()

    def test_each_auth_endpoint_throttles_and_recovers(self):
        endpoints = {'login': 'login', 'register': 'register',
                     'refresh': 'token/refresh', 'social_login': 'social'}
        for scope, path in endpoints.items():
            with self.subTest(scope=scope), patch.object(
                    ScopedRateThrottle, 'THROTTLE_RATES', {scope: '2/min'}):
                cache.clear()
                with patch.object(ScopedRateThrottle, 'timer', return_value=1000):
                    for _ in range(2):
                        self.assertNotEqual(self.client.post(f'/api/users/{path}/', {}).status_code, 429)
                    denied = self.client.post(f'/api/users/{path}/', {})
                    self.assertEqual(denied.status_code, 429)
                    self.assertIn('Retry-After', denied)
                with patch.object(ScopedRateThrottle, 'timer', return_value=1061):
                    self.assertNotEqual(self.client.post(f'/api/users/{path}/', {}).status_code, 429)

    def test_forwarded_header_does_not_rotate_login_budget(self):
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'login': '1/min'}):
            self.client.post('/api/users/login/', {}, HTTP_X_FORWARDED_FOR='203.0.113.1')
            self.assertEqual(self.client.post('/api/users/login/', {},
                HTTP_X_FORWARDED_FOR='203.0.113.2').status_code, 429)
            self.assertNotEqual(self.client.post('/api/users/login/', {},
                REMOTE_ADDR='198.51.100.2').status_code, 429)

    def test_password_failure_does_not_disclose_account_existence(self):
        User.objects.create_user(username='known', email='known@example.test', password='correct-pass')
        responses = [self.client.post('/api/users/login/', {
            'email': email, 'password': 'wrong-pass',
        }) for email in ('known@example.test', 'missing@example.test')]
        self.assertEqual([r.status_code for r in responses], [401, 401])
        self.assertEqual(responses[0].data, responses[1].data)

    def test_login_budget_does_not_consume_social_budget(self):
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES',
                          {'login': '1/min', 'social_login': '1/min'}):
            self.client.post('/api/users/login/', {})
            self.assertNotEqual(self.client.post('/api/users/social/', {}).status_code, 429)
