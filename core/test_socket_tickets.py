from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import logging

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from core.middleware import SocketTicketMiddleware
from core.credential_logging import CredentialRedactionFilter
from core.socket_tickets import TICKET_TTL, TicketUnavailable, issue_ticket, redeem_ticket
from friends.routing import websocket_urlpatterns
from users.models import User


@override_settings(DEBUG=True)
class SocketTicketTests(TransactionTestCase):
    path = '/ws/notifications/'

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='ticket', email='ticket@example.test')
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(self.user)}')

    def test_endpoint_requires_authentication_and_never_returns_a_jwt(self):
        response = APIClient().post('/api/socket-ticket/', {'path': self.path}, format='json')
        self.assertEqual(response.status_code, 401)
        response = self.client.post('/api/socket-ticket/', {'path': self.path}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.data['ticket'], r'^[A-Za-z0-9_-]{43}$')
        self.assertEqual(response.data['expires_in'], TICKET_TTL)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(redeem_ticket(response.data['ticket'], self.path).pk, self.user.pk)

    def test_endpoint_rejects_invalid_paths_and_shapes(self):
        for data in ({}, [], {'path': None}, {'path': '/admin/'}, {'path': self.path + '?token=x'},
                     {'path': '/ws/chat/-1/'}, {'path': '/ws/clubs/0/'}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post('/api/socket-ticket/', data, format='json').status_code, 400)

    def test_ticket_is_single_use_and_bound_to_one_path(self):
        ticket = issue_ticket(self.user.pk, self.path)
        self.assertIsNone(redeem_ticket(ticket, '/ws/chat/123/'))
        self.assertEqual(redeem_ticket(ticket, self.path).pk, self.user.pk)
        self.assertIsNone(redeem_ticket(ticket, self.path))

    def test_expiry_is_checked_even_if_cache_has_not_evicted_the_key(self):
        with patch('core.socket_tickets.time.time', return_value=1000):
            ticket = issue_ticket(self.user.pk, self.path)
        with patch('core.socket_tickets.time.time', return_value=1000 + TICKET_TTL):
            self.assertIsNone(redeem_ticket(ticket, self.path))

    def test_account_standing_is_reloaded_at_redemption_and_issuance(self):
        for fields in (
            {'is_active': False}, {'suspended_at': timezone.now()},
            {'deletion_requested_at': timezone.now()}, {'retired_account_id': 99999},
        ):
            ticket = issue_ticket(self.user.pk, self.path)
            User.objects.filter(pk=self.user.pk).update(**fields)
            self.assertIsNone(redeem_ticket(ticket, self.path))
            self.assertIn(self.client.post('/api/socket-ticket/', {'path': self.path}, format='json').status_code, (401, 403))
            User.objects.filter(pk=self.user.pk).update(
                is_active=True, suspended_at=None, deletion_requested_at=None, retired_account_id=None)
            self.assertIsNone(redeem_ticket(ticket, self.path))

    def test_cache_errors_fail_closed_without_leaking_credentials(self):
        with patch('core.socket_tickets.cache.add', side_effect=RuntimeError('secret-cache-url')):
            with self.assertLogs('core.socket_tickets', level='WARNING') as logs:
                response = self.client.post('/api/socket-ticket/', {'path': self.path}, format='json')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('secret-cache-url', str(logs.output) + str(response.data))
        ticket = issue_ticket(self.user.pk, self.path)
        with patch('core.socket_tickets._take_ticket', side_effect=RuntimeError('secret')):
            with self.assertLogs('core.socket_tickets', level='WARNING'):
                self.assertIsNone(redeem_ticket(ticket, self.path))
        with patch('core.socket_tickets.cache.get', side_effect=RuntimeError('secret-cache-url')):
            with self.assertLogs('core.socket_tickets', level='WARNING'):
                self.assertEqual(self.client.post('/api/socket-ticket/', {'path': self.path}, format='json').status_code, 503)

    def test_server_access_logs_redact_tickets_and_legacy_jwts(self):
        for name in ('uvicorn.access', 'uvicorn.error'):
            for query in ('ticket=opaque-credential', 'token=legacy-jwt'):
                record = logging.LogRecord(name, logging.INFO, '', 0,
                    '%s - "%s %s HTTP/%s" %d', ('127.0.0.1', 'GET', f'{self.path}?{query}', '1.1', 101), None)
                CredentialRedactionFilter().filter(record)
                self.assertNotIn(query.split('=')[1], record.getMessage())
                self.assertIn('[redacted]', record.getMessage())
                self.assertEqual(len(record.args), 5)

    @override_settings(DEBUG=False, CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
    def test_production_refuses_process_local_cache(self):
        with self.assertRaises(TicketUnavailable):
            issue_ticket(self.user.pk, self.path)
        with self.assertLogs('core.socket_tickets', level='WARNING'):
            self.assertIsNone(redeem_ticket('a' * 43, self.path))

    def test_endpoint_is_rate_limited(self):
        with patch('core.socket_tickets.ScopedRateThrottle.get_rate', return_value='1/min'):
            self.assertEqual(self.client.post('/api/socket-ticket/', {'path': self.path}, format='json').status_code, 200)
            self.assertEqual(self.client.post('/api/socket-ticket/', {'path': self.path}, format='json').status_code, 429)

    def test_concurrent_redemption_has_exactly_one_winner(self):
        if settings.CACHES['default']['BACKEND'] != 'django.core.cache.backends.redis.RedisCache':
            self.skipTest('Run with core.ci_test_settings for real shared Redis atomicity.')
        ticket = issue_ticket(self.user.pk, self.path)
        barrier = Barrier(8)

        def redeem():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return redeem_ticket(ticket, self.path) is not None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(lambda _: redeem(), range(8))), 1)

    async def test_middleware_refuses_jwts_duplicate_tickets_and_malformed_queries(self):
        app = SocketTicketMiddleware(URLRouter(websocket_urlpatterns))
        jwt = str(AccessToken.for_user(self.user))
        ticket = issue_ticket(self.user.pk, self.path)
        for query in (f'token={jwt}', f'ticket={jwt}', f'ticket={ticket}&ticket={ticket}',
                      f'ticket={ticket}&token={jwt}', 'ticket=', 'ticket=' + 'x' * 200):
            socket = WebsocketCommunicator(app, f'{self.path}?{query}')
            self.assertEqual(await socket.connect(), (False, 4001))
            await socket.disconnect()
        socket = WebsocketCommunicator(app, f'{self.path}?ticket={ticket}')
        self.assertTrue((await socket.connect())[0])
        await socket.disconnect()
        replay = WebsocketCommunicator(app, f'{self.path}?ticket={ticket}')
        self.assertEqual(await replay.connect(), (False, 4001))
        await replay.disconnect()
