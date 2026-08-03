"""Caching, coalescing and rate limiting in front of Nominatim.

The failure these exist to prevent: a burst of cold lookups all queue behind
one global 1.1s slot, each holding a request open, and the ones at the back time
out having never reached the network. Coalescing removes the duplicates,
caching removes the repeats, and separate budgets stop a queue eating the time
the request itself needs.

Threads here are real threads against the process-local cache, which is what
LocMemCache is in tests. That exercises the leader election for genuine
concurrency; across gunicorn workers the same election runs on Redis, which is
why it is built on `cache.add` rather than an in-process lock.
"""
import threading
import time
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from . import nominatim
from .tests import make_region, make_user

SEARCH = {'q': 'almaty', 'format': 'jsonv2'}
OTHER = {'q': 'astana', 'format': 'jsonv2'}
FOUND = [{'osm_id': 1, 'name': 'Almaty'}]


def run_together(target, count):
    """Fire `count` threads at `target` and collect what each got back.

    Results come back as (index, value) or (index, exception) so a failure in a
    worker surfaces as a test failure rather than a silent miss.
    """
    results = {}
    lock = threading.Lock()

    def worker(index):
        try:
            value = target()
        except Exception as exc:  # noqa: BLE001 — recorded, then re-asserted
            value = exc
        with lock:
            results[index] = value

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    return threads, results


class CoalescingTests(SimpleTestCase):
    """Concurrent callers asking the identical question must cost one request."""

    def setUp(self):
        cache.clear()

    def test_identical_cold_requests_make_one_upstream_call(self):
        calls = []
        release = threading.Event()

        def slow(path, params):
            calls.append(params)
            release.wait(timeout=5)
            return FOUND

        with patch('regions.nominatim._get', side_effect=slow):
            threads, results = run_together(
                lambda: nominatim._fetch('search', SEARCH), 5,
            )
            # Give every follower time to find the leader already in flight.
            time.sleep(0.3)
            release.set()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(len(calls), 1, 'one upstream call for five identical asks')
        self.assertEqual(len(results), 5)
        for value in results.values():
            self.assertEqual(value, FOUND)

    def test_different_cold_requests_each_go_upstream(self):
        """Coalescing keys on the exact question. Two different ones must not
        collapse into each other, however close together they arrive."""
        calls = []
        lock = threading.Lock()

        def record(path, params):
            with lock:
                calls.append(params['q'])
            return [{'osm_id': len(calls), 'name': params['q']}]

        with patch('regions.nominatim._get', side_effect=record):
            threads, _ = run_together(
                lambda: nominatim._fetch('search', dict(SEARCH)), 1,
            )
            more, _ = run_together(
                lambda: nominatim._fetch('search', dict(OTHER)), 1,
            )
            for thread in [*threads, *more]:
                thread.join(timeout=10)

        self.assertEqual(sorted(calls), ['almaty', 'astana'])

    def test_a_slow_upstream_is_waited_on_rather_than_duplicated(self):
        """The leader takes most of a second; the followers must sit through it
        rather than each starting their own."""
        calls = []
        release = threading.Event()

        def slow(path, params):
            calls.append(params)
            release.wait(timeout=5)
            return FOUND

        with patch('regions.nominatim._get', side_effect=slow):
            threads, results = run_together(
                lambda: nominatim._fetch('search', SEARCH), 3,
            )
            time.sleep(0.6)
            self.assertEqual(len(calls), 1, 'nobody else started a request')
            release.set()
            for thread in threads:
                thread.join(timeout=10)

        self.assertTrue(all(value == FOUND for value in results.values()))


class ResponseCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_a_successful_answer_is_reused(self):
        with patch('regions.nominatim._get', return_value=FOUND) as get:
            first = nominatim._fetch('search', SEARCH)
            second = nominatim._fetch('search', SEARCH)
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)

    def test_a_not_found_answer_is_reused_but_expires_sooner(self):
        """"Nothing is mapped here" is a real answer and worth caching. It is
        also a statement about OSM's coverage today, and coverage grows — so it
        must not be kept alongside borders, which do not move."""
        with patch('regions.nominatim._get', return_value=[]) as get:
            self.assertEqual(nominatim._fetch('search', SEARCH), [])
            self.assertEqual(nominatim._fetch('search', SEARCH), [])
        self.assertEqual(get.call_count, 1)

        envelope = cache.get(nominatim._cache_key('search', SEARCH))
        self.assertTrue(envelope['missing'])
        self.assertLess(
            nominatim._conf('MISS_TTL_S', 0), nominatim._conf('CACHE_TTL_S', 0),
        )

    def test_a_reverse_error_body_counts_as_not_found(self):
        with patch('regions.nominatim._get', return_value={'error': 'Unable to geocode'}):
            nominatim._fetch('reverse', {'lat': 1, 'lon': 2})
        envelope = cache.get(nominatim._cache_key('reverse', {'lat': 1, 'lon': 2}))
        self.assertTrue(envelope['missing'])

    def test_a_transient_failure_is_not_stored_as_a_miss(self):
        """The distinction that matters: a 502 must never become "no district
        is mapped here", which is what a shared miss cache would turn it into."""
        with patch('regions.nominatim._get', side_effect=nominatim.NominatimError('502')):
            with self.assertRaises(nominatim.NominatimError):
                nominatim._fetch('search', SEARCH)

        envelope = cache.get(nominatim._cache_key('search', SEARCH))
        self.assertIn('error', envelope)
        self.assertNotIn('payload', envelope)

    @override_settings(NOMINATIM_ERROR_TTL_S=0)
    def test_a_transient_failure_does_not_survive(self):
        """With the marker expired, the next caller goes back upstream — the
        marker exists to break a stampede, not to remember a verdict."""
        with patch('regions.nominatim._get', side_effect=nominatim.NominatimError('502')) as get:
            with self.assertRaises(nominatim.NominatimError):
                nominatim._fetch('search', SEARCH)
            with self.assertRaises(nominatim.NominatimError):
                nominatim._fetch('search', SEARCH)
        self.assertEqual(get.call_count, 2)

    def test_an_upstream_timeout_surfaces_as_an_error_not_an_empty_answer(self):
        with patch('regions.nominatim._throttle', return_value=0.0):
            with patch('regions.nominatim.requests.get', side_effect=requests.Timeout()):
                with self.assertRaises(nominatim.NominatimError) as caught:
                    nominatim._fetch('search', SEARCH)
        self.assertIn('timed out', str(caught.exception))
        self.assertIn('error', cache.get(nominatim._cache_key('search', SEARCH)))

    def test_a_timeout_is_not_reported_as_capacity(self):
        """A slow upstream and a full queue are different problems, and only
        one of them is worth retrying in a second."""
        with patch('regions.nominatim._throttle', return_value=0.0):
            with patch('regions.nominatim.requests.get', side_effect=requests.Timeout()):
                with self.assertRaises(nominatim.NominatimError) as caught:
                    nominatim._fetch('search', SEARCH)
        self.assertNotIsInstance(caught.exception, nominatim.NominatimBusy)


class QueueBudgetTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @override_settings(NOMINATIM_MAX_QUEUE_WAIT_S=0.05)
    def test_an_exhausted_queue_raises_something_retryable(self):
        with patch('regions.nominatim._claim_slot', return_value=False):
            with self.assertRaises(nominatim.NominatimBusy) as caught:
                nominatim._throttle()
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    @override_settings(NOMINATIM_MAX_QUEUE_WAIT_S=30, NOMINATIM_TIMEOUT_S=8)
    def test_the_queue_budget_is_not_the_network_budget(self):
        """A caller that queued must still arrive at the request with a full
        network timeout, or a queue and a slow upstream compound into a deadline
        nobody can meet."""
        seen = {}

        def capture(url, params, headers, timeout):
            seen['timeout'] = timeout
            raise requests.Timeout()

        with patch('regions.nominatim._throttle', return_value=29.0):
            with patch('regions.nominatim.requests.get', side_effect=capture):
                with self.assertRaises(nominatim.NominatimError):
                    nominatim._get('search', SEARCH)
        self.assertEqual(seen['timeout'], 8)

    @override_settings(NOMINATIM_MIN_INTERVAL_S=0)
    def test_throttling_can_be_switched_off_for_a_self_hosted_instance(self):
        with patch('regions.nominatim._claim_slot', return_value=False) as claim:
            self.assertEqual(nominatim._throttle(), 0.0)
        claim.assert_not_called()

    @override_settings(NOMINATIM_MAX_CONCURRENCY=3)
    def test_concurrency_is_configurable(self):
        """Three slots means three callers proceed and the fourth waits — the
        same election, widened for an instance whose policy allows it."""
        self.assertTrue(nominatim._claim_slot())
        self.assertTrue(nominatim._claim_slot())
        self.assertTrue(nominatim._claim_slot())
        self.assertFalse(nominatim._claim_slot())


class BusyResponseTests(TestCase):
    """What a caller out of capacity actually receives."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(make_user('me'))

    @override_settings(NOMINATIM_MAX_QUEUE_WAIT_S=0.05)
    def test_for_point_answers_with_a_retry_after(self):
        with patch('regions.nominatim._claim_slot', return_value=False):
            response = self.client.get('/api/regions/for-point/?lat=0.5&lon=0.5')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response['Retry-After'], str(response.json()['retry_after']))
        self.assertIn('moment', response.json()['detail'])

    @override_settings(NOMINATIM_MAX_QUEUE_WAIT_S=0.05)
    def test_search_answers_with_a_retry_after(self):
        with patch('regions.nominatim._claim_slot', return_value=False):
            response = self.client.get('/api/regions/search/?q=nowhere')
        self.assertEqual(response.status_code, 503)
        self.assertIn('Retry-After', response)

    @override_settings(NOMINATIM_MAX_QUEUE_WAIT_S=0.05)
    def test_cached_rows_still_answer_a_search_while_busy(self):
        """Being out of capacity is not being out of answers."""
        make_region(osm_id=4001, name='Almaty', display_name='Almaty, KZ')
        with patch('regions.nominatim._claim_slot', return_value=False):
            response = self.client.get('/api/regions/search/?q=almaty')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_a_broken_upstream_is_a_503_without_a_retry_after(self):
        """Nothing to promise: this is not a queue that is about to clear."""
        with patch(
            'regions.nominatim.address_at',
            side_effect=nominatim.NominatimError('upstream on fire'),
        ):
            response = self.client.get('/api/regions/for-point/?lat=0.5&lon=0.5')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('Retry-After', response)


class SearchKindsFilterTests(TestCase):
    """Search may be narrowed to the sizes a zone can actually be.

    Without it the picker offers provinces and countries, and one of those is a
    legal zone as far as the containment rule is concerned — you are inside your
    own country.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(make_user('me'))
        make_region(osm_id=4101, name='Almaty', display_name='Almaty, KZ', kind='city')
        make_region(
            osm_id=4102, name='Almaty Region', display_name='Almaty Region, KZ',
            kind='other',
        )

    def test_without_the_parameter_nothing_is_filtered(self):
        with patch('regions.nominatim.search', return_value=[]):
            body = self.client.get('/api/regions/search/?q=almaty').json()
        self.assertEqual(len(body), 2)

    def test_the_filter_drops_everything_that_is_not_a_zone_size(self):
        with patch('regions.nominatim.search', return_value=[]):
            body = self.client.get(
                '/api/regions/search/?q=almaty&kinds=city,district'
            ).json()
        self.assertEqual([row['name'] for row in body], ['Almaty'])

    def test_an_unrecognised_kind_widens_rather_than_400s(self):
        with patch('regions.nominatim.search', return_value=[]):
            response = self.client.get('/api/regions/search/?q=almaty&kinds=planet')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
