from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recs.exposure import mark_served
from recs.models import SwiftImpression
from swifts.testing import SwiftTestCase, make_swift, make_user


class ImpressionWriteTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def test_new_repeated_and_mixed_batches_preserve_counts_and_timestamps(self):
        first, second = make_swift(self.author), make_swift(self.author)
        now = timezone.now()
        with patch('recs.exposure.timezone.now', return_value=now):
            mark_served(self.viewer, [(first.pk, 'friends')])
            self.assertEqual(SwiftImpression.objects.get(swift=first).serve_count, 1)
            # Exact same timestamp must still count the next serve.
            mark_served(self.viewer, [(first.pk, 'explore')])
        later = now + timedelta(hours=1)
        with patch('recs.exposure.timezone.now', return_value=later):
            mark_served(self.viewer, [(first.pk, 'nearby'), (second.pk, "it's new")])
        old = SwiftImpression.objects.get(swift=first)
        new = SwiftImpression.objects.get(swift=second)
        self.assertEqual((old.serve_count, old.first_served_at, old.last_served_at, old.served_by),
                         (3, now, later, 'nearby'))
        self.assertEqual((new.serve_count, new.served_by), (1, "it's new"))

    def test_page_has_one_write_and_duplicate_ids_count_once(self):
        entries = [(make_swift(self.author).pk, 'friends') for _ in range(50)]
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(mark_served(self.viewer, entries + entries), 50)
        writes = [q['sql'] for q in queries if q['sql'].startswith(('INSERT', 'UPDATE'))]
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(SwiftImpression.objects.values_list('serve_count', flat=True)), {1})
        self.assertEqual(mark_served(self.viewer, []), 0)


@skipUnless(connection.vendor == 'postgresql', 'Concurrent impression regression requires PostgreSQL.')
class ConcurrentImpressionTests(TransactionTestCase):
    def test_simultaneous_serves_increment_without_lost_updates(self):
        viewer = make_user('concurrent-viewer')
        swift = make_swift(make_user('concurrent-author'))
        barrier = Barrier(4)

        def serve():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return mark_served(viewer, [(swift.pk, 'friends')])
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: serve(), range(4))), [1] * 4)
        self.assertEqual(SwiftImpression.objects.get().serve_count, 4)
