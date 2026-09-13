from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from .models import SwiftVideo
from .testing import SwiftTestCase, fake_video, make_user


class PublishIntegrityTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.author = make_user('publisher')
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def publish(self, key, **extra):
        return self.client.post('/api/swifts/', {
            'video': fake_video(), 'duration_ms': 5000,
            'idempotency_key': str(key), **extra,
        }, format='multipart')

    def test_same_take_reuses_row_media_and_job_even_if_response_was_lost(self):
        key = uuid4()
        storage = SwiftVideo._meta.get_field('video').storage
        with patch.object(storage, 'save', wraps=storage.save) as save, \
                patch('swifts.views.ai_moderation.enqueue') as enqueue:
            first = self.publish(key, caption='original')
            repeated = self.publish(key, caption='changed payload')
            key_only = self.client.post('/api/swifts/', {'idempotency_key': str(key)})
        self.assertEqual([first.status_code, repeated.status_code, key_only.status_code], [201, 200, 200])
        self.assertEqual(first.data, repeated.data)
        self.assertEqual(first.data, key_only.data)
        self.assertEqual(SwiftVideo.objects.count(), 1)
        save.assert_called_once()
        enqueue.assert_called_once()
        self.assertNotIn('idempotency_key', first.data)

    def test_distinct_takes_and_authors_are_independent(self):
        key = uuid4()
        first = self.publish(key)
        second = self.publish(uuid4())
        self.client.force_authenticate(make_user('other-publisher'))
        other = self.publish(key)
        self.assertEqual([first.status_code, second.status_code, other.status_code], [201, 201, 201])
        self.assertEqual(len({first.data['id'], second.data['id'], other.data['id']}), 3)

    def test_database_enforces_author_key_uniqueness(self):
        key = uuid4()
        SwiftVideo.objects.create(author=self.author, idempotency_key=key)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SwiftVideo.objects.create(author=self.author, idempotency_key=key)

    def test_invalid_first_request_does_not_consume_key_and_legacy_posts_still_work(self):
        key = uuid4()
        self.assertEqual(self.publish('not-a-uuid').status_code, 400)
        self.assertEqual(self.client.post('/api/swifts/', {'idempotency_key': str(key)}).status_code, 400)
        self.assertEqual(self.publish(key).status_code, 201)
        for _ in range(2):
            self.assertEqual(self.client.post('/api/swifts/', {
                'video': fake_video(), 'duration_ms': 5000,
            }, format='multipart').status_code, 201)
        self.assertEqual(SwiftVideo.objects.count(), 3)

    def test_retry_cannot_resurrect_deleted_or_removed_take(self):
        for fields in ({'deleted_at': timezone.now()}, {'moderation': 'removed'}):
            key = uuid4()
            first = self.publish(key)
            SwiftVideo.objects.filter(pk=first.data['id']).update(**fields)
            self.assertEqual(self.publish(key).status_code, 404)
            self.assertEqual(SwiftVideo.objects.filter(idempotency_key=key).count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'Concurrent publish regression requires PostgreSQL row locks.')
class ConcurrentPublishTests(TransactionTestCase):
    def test_concurrent_retries_store_one_file_row_and_job(self):
        cache.clear()
        author = make_user('concurrent-publisher')
        key = str(uuid4())
        barrier = Barrier(2)

        def publish():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=author.pk))
                barrier.wait(timeout=10)
                response = client.post('/api/swifts/', {
                    'video': fake_video(), 'duration_ms': 5000, 'idempotency_key': key,
                }, format='multipart')
                return response.status_code, response.data['id']
            finally:
                close_old_connections()

        storage = SwiftVideo._meta.get_field('video').storage
        with patch.object(storage, 'save', wraps=storage.save) as save, \
                patch('swifts.views.ai_moderation.enqueue') as enqueue, \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: publish(), range(2)))
        self.assertEqual(sorted(code for code, _ in results), [200, 201])
        self.assertEqual(len({pk for _, pk in results}), 1)
        self.assertEqual(SwiftVideo.objects.count(), 1)
        save.assert_called_once()
        enqueue.assert_called_once()
