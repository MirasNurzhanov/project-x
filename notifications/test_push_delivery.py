from unittest.mock import Mock, patch

import requests
from django.test import TestCase, SimpleTestCase, override_settings
from rest_framework.test import APIClient

from notifications import push
from notifications.dead_tokens import forget_tokens
from users.models import Device, User

DEAD = 'ExponentPushToken[dead-test]'
LIVE = 'ExpoPushToken[live-test]'


class TicketTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ticket-test', push_token=DEAD)
        self.device = Device.objects.create(user=self.user, install_id='stable-install',
                                            name='Test phone', platform='ios', push_token=DEAD)

    def send(self, payload, messages=None):
        response = Mock()
        response.json.return_value = payload
        with patch('notifications.push.requests.post', return_value=response):
            push._post(messages or [{'to': DEAD}])

    def test_dead_ticket_clears_token_but_preserves_identity_and_live_devices(self):
        live = Device.objects.create(user=self.user, install_id='second', push_token=LIVE)
        self.send({'data': [
            {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}},
            {'status': 'ok', 'id': 'ticket-id'},
        ]}, [{'to': DEAD}, {'to': LIVE}])
        self.device.refresh_from_db()
        self.user.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(self.device.push_token, '')
        self.assertEqual(self.user.push_token, '')
        self.assertEqual((self.device.install_id, self.device.name, self.device.platform),
                         ('stable-install', 'Test phone', 'ios'))
        self.assertEqual(live.push_token, LIVE)
        self.assertEqual(Device.objects.count(), 2)

    def test_rotated_token_is_not_cleared_by_delayed_old_ticket(self):
        Device.objects.filter(pk=self.device.pk).update(push_token=LIVE)
        User.objects.filter(pk=self.user.pk).update(push_token=LIVE)
        self.assertEqual(forget_tokens([DEAD]), 0)
        self.device.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.device.push_token, LIVE)
        self.assertEqual(self.user.push_token, LIVE)

    def test_malformed_or_other_error_never_removes_token(self):
        for payload in ({}, {'data': []}, {'data': [None]}, {'data': [
                {'status': 'error', 'details': {'error': 'MessageTooBig'}}]}):
            self.send(payload)
            self.device.refresh_from_db()
            self.assertEqual(self.device.push_token, DEAD)

    def test_network_and_database_failures_are_nonfatal_and_do_not_log_secrets(self):
        with patch('notifications.push.requests.post', side_effect=requests.Timeout(DEAD)), \
                self.assertLogs('notifications.push', level='WARNING') as logs:
            push._post([{'to': DEAD, 'body': 'private message'}])
        self.assertNotIn(DEAD, str(logs.output))
        response = Mock()
        response.json.return_value = {'data': {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}}}
        with patch('notifications.push.requests.post', return_value=response), \
                patch('notifications.push.forget_tokens', side_effect=RuntimeError(DEAD)), \
                self.assertLogs('notifications.push', level='ERROR') as logs:
            push._post([{'to': DEAD}])
        self.assertNotIn(DEAD, str(logs.output))

    @override_settings(PUSH_DISPATCHER_TOKEN='test-shared-secret')
    def test_callback_requires_shared_secret_and_validated_list(self):
        client = APIClient()
        url = '/api/internal/push/dead-tokens/'
        self.assertEqual(client.post(url, {'tokens': [DEAD]}, format='json').status_code, 403)
        client.force_authenticate(self.user)
        self.assertEqual(client.post(url, {'tokens': [DEAD]}, format='json').status_code, 403)
        client.credentials(HTTP_AUTHORIZATION='Bearer test-shared-secret')
        for tokens in ([], ['not-a-token'], [DEAD] * 101):
            self.assertEqual(client.post(url, {'tokens': tokens}, format='json').status_code, 400)
        response = client.post(url, {'tokens': [DEAD]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'cleared_devices': 1})
        self.assertEqual(client.post(url, {'tokens': [DEAD]}, format='json').data, {'cleared_devices': 0})
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())

    @override_settings(PUSH_DISPATCHER_TOKEN='')
    def test_unconfigured_callback_fails_closed(self):
        response = APIClient().post('/api/internal/push/dead-tokens/', {'tokens': [DEAD]}, format='json',
                                    HTTP_AUTHORIZATION='Bearer ')
        self.assertEqual(response.status_code, 403)


class PushPoolTests(SimpleTestCase):
    @override_settings(PUSH_DISABLED=False, PUSH_DISPATCHER_URL='')
    def test_full_queue_returns_without_starting_more_work(self):
        with patch('notifications.push._slots') as slots, patch('notifications.push._executor') as executor:
            slots.acquire.return_value = False
            self.assertEqual(push._queue([{'to': DEAD}]), 0)
            executor.submit.assert_not_called()

    def test_worker_always_releases_slot_and_database_connections(self):
        with patch('notifications.push._slots') as slots, \
                patch('django.db.close_old_connections'), patch('django.db.connections.close_all') as close, \
                patch('notifications.push._post', side_effect=RuntimeError('failure')):
            push._deliver([{'to': DEAD}])
            close.assert_called_once()
            slots.release.assert_called_once()
