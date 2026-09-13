from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from users.models import User
from .models import BlockedUser, HiRequest


class HiAbuseTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.sender = User.objects.create_user(username='sender', email='sender@example.test')
        self.receiver = User.objects.create_user(username='receiver', email='receiver@example.test')
        self.client = APIClient()
        self.client.force_authenticate(self.sender)

    def send(self, receiver_id=None):
        return self.client.post('/api/friends/hi/', {
            'receiver_id': self.receiver.pk if receiver_id is None else receiver_id,
        }, format='json')

    def test_repeats_keep_identity_and_only_notify_once(self):
        with patch('friends.views.send_push_notification') as push, self.captureOnCommitCallbacks(execute=True):
            first, repeat = self.send(), self.send()
        self.assertEqual([first.status_code, repeat.status_code], [201, 200])
        self.assertEqual(first.data['id'], repeat.data['id'])
        self.assertEqual(HiRequest.objects.count(), 1)
        push.assert_called_once()

    def test_accepted_and_declined_requests_cannot_be_reset_by_sender(self):
        for outcome in ('accept', 'decline'):
            with self.subTest(outcome=outcome):
                HiRequest.objects.all().delete()
                hi = self.send()
                self.client.force_authenticate(self.receiver)
                response = self.client.post(f'/api/friends/hi/{hi.data["id"]}/{outcome}/')
                self.assertEqual(response.status_code, 200 if outcome == 'accept' else 204)
                self.client.force_authenticate(self.sender)
                with patch('friends.views.send_push_notification') as push, self.captureOnCommitCallbacks(execute=True):
                    repeated = self.send()
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.data['status'], 'accepted' if outcome == 'accept' else 'rejected')
                push.assert_not_called()

    def test_hidden_missing_and_invalid_ids_are_indistinguishable(self):
        missing = self.send(999999)
        for blocker, blocked in ((self.sender, self.receiver), (self.receiver, self.sender)):
            block = BlockedUser.objects.create(blocker=blocker, blocked=blocked)
            hidden = self.send()
            self.assertEqual((hidden.status_code, hidden.data), (missing.status_code, missing.data))
            block.delete()
        for target in ('invalid', [1], 10 ** 100, self.sender.pk):
            res = self.send(target)
            self.assertEqual((res.status_code, res.data), (missing.status_code, missing.data))

    def test_social_request_threshold(self):
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'social_request': '2/min'}):
            self.assertEqual(self.send().status_code, 201)
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send().status_code, 429)
            self.assertEqual(self.client.post(
                f'/api/friends/request/{self.receiver.pk}/').status_code, 429)


@skipUnless(connection.vendor == 'postgresql', 'Concurrent writers require PostgreSQL; SQLite locks the database.')
class ConcurrentHiTests(TransactionTestCase):
    def test_simultaneous_first_sends_create_one_row_and_one_notification(self):
        sender = User.objects.create_user(username='sender', email='sender@example.test')
        receiver = User.objects.create_user(username='receiver', email='receiver@example.test')
        barrier = Barrier(2)

        def send():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=sender.pk))
                barrier.wait(timeout=5)
                return client.post('/api/friends/hi/', {'receiver_id': receiver.pk}).status_code
            finally:
                close_old_connections()

        cache.clear()
        with patch('friends.views.send_push_notification') as push, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(sorted(results), [200, 201])
        self.assertEqual(HiRequest.objects.count(), 1)
        push.assert_called_once()
