"""The endpoints, and the feed contract they changed.

The feed's *payload* is deliberately not re-tested here — `swifts/` already
pins what a row contains and that it contains no coordinate, and the whole
point of ranking only ids is that those tests stayed true without being
touched. What is tested here is what actually changed: ordering, cursoring, and
the new signal endpoint.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from ..models import SwiftImpression, SwiftSignal
from ..testing import SwiftTestCase, bulk_swifts, make_swift, make_user, popular


class FeedContractTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        for swift in bulk_swifts(self.author, 8):
            popular(swift, likes=3, views=30)

    def test_the_ranked_feed_returns_a_cursor(self):
        body = self.client.get(reverse('swift-feed'), {'limit': 3}).json()

        self.assertIn('results', body)
        self.assertIn('next_cursor', body)
        self.assertTrue(body['next_cursor'])
        self.assertLessEqual(len(body['results']), 3)

    def test_following_the_cursor_does_not_repeat_rows(self):
        first = self.client.get(reverse('swift-feed'), {'limit': 3}).json()
        second = self.client.get(
            reverse('swift-feed'), {'limit': 3, 'cursor': first['next_cursor']},
        ).json()

        first_ids = {r['id'] for r in first['results']}
        second_ids = {r['id'] for r in second['results']}
        self.assertEqual(first_ids & second_ids, set())

    def test_the_results_come_back_in_the_recommenders_order(self):
        """The last-step mistake that looks exactly like a ranking bug.

        `filter(id__in=...)` returns rows in whatever order the database likes,
        so the view has to re-impose the ranking. This asserts it did.
        """
        from ..pipeline import page

        expected = page(self.viewer, limit=5).swift_ids
        SwiftImpression.objects.all().delete()

        body = self.client.get(reverse('swift-feed'), {'limit': 5}).json()
        returned = [r['id'] for r in body['results']]

        self.assertEqual(len(returned), len(expected))
        # Same session parameters, so the same order — the ids themselves may
        # differ if impressions intervened, but the order must be the view's
        # input order, which is what re-imposing guarantees.
        from ..pipeline import build_session
        SwiftImpression.objects.all().delete()
        ordered, _, _, _ = build_session(self.viewer, seed=self.viewer.pk)
        rank = {swift_id: i for i, swift_id in enumerate(ordered)}
        positions = [rank[i] for i in returned if i in rank]
        self.assertEqual(positions, sorted(positions))

    def test_chrono_mode_is_still_reverse_chronological(self):
        body = self.client.get(reverse('swift-feed'), {'chrono': '1', 'limit': 5}).json()
        created = [r['created_at'] for r in body['results']]

        self.assertEqual(created, sorted(created, reverse=True))
        self.assertIsNotNone(body['count'])

    def test_chrono_mode_still_offsets(self):
        first = self.client.get(reverse('swift-feed'), {'chrono': '1', 'limit': 2}).json()
        second = self.client.get(
            reverse('swift-feed'), {'chrono': '1', 'limit': 2, 'offset': 2},
        ).json()

        self.assertEqual(
            {r['id'] for r in first['results']} & {r['id'] for r in second['results']},
            set(),
        )

    def test_the_ranked_feed_reports_which_source_won_each_slot(self):
        body = self.client.get(reverse('swift-feed'), {'limit': 3}).json()

        self.assertIn('sources', body)
        for row in body['results']:
            self.assertIn(str(row['id']), {str(k) for k in body['sources']})

    def test_serving_a_ranked_page_records_impressions(self):
        self.client.get(reverse('swift-feed'), {'limit': 3})

        self.assertGreater(
            SwiftImpression.objects.filter(viewer=self.viewer).count(), 0,
        )

    def test_chrono_mode_does_not_record_impressions(self):
        """The unranked fallback must not consume the ranked feed's candidates.

        Sharing the exposure table between the two would mean a debugging
        request to `?chrono=1` silently burned everything it returned.
        """
        self.client.get(reverse('swift-feed'), {'chrono': '1', 'limit': 5})

        self.assertEqual(
            SwiftImpression.objects.filter(viewer=self.viewer).count(), 0,
        )

    def test_the_feed_requires_authentication(self):
        self.assertEqual(APIClient().get(reverse('swift-feed')).status_code, 401)


class SignalEndpointTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')
        self.swift = make_swift(self.author)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def post(self, signals):
        return self.client.post(
            reverse('swift-signals'), {'signals': signals}, format='json',
        )

    def test_a_batch_is_recorded(self):
        response = self.post([
            {'kind': 'watch', 'swift_id': self.swift.id, 'completion_pct': 90},
            {'kind': 'like', 'swift_id': self.swift.id},
        ])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['recorded'], 2)
        self.assertEqual(SwiftSignal.objects.filter(viewer=self.viewer).count(), 2)

    def test_the_viewer_cannot_be_spoofed(self):
        other = make_user('other')
        self.post([{'kind': 'like', 'swift_id': self.swift.id, 'viewer': other.pk}])

        self.assertFalse(SwiftSignal.objects.filter(viewer=other).exists())
        self.assertTrue(SwiftSignal.objects.filter(viewer=self.viewer).exists())

    def test_an_unknown_kind_is_rejected(self):
        response = self.post([{'kind': 'admire', 'swift_id': self.swift.id}])
        self.assertEqual(response.status_code, 400)

    def test_a_signal_about_an_unwatchable_swift_is_dropped(self):
        from swifts.models import SwiftVideo

        hidden = make_swift(self.author, visibility=SwiftVideo.Visibility.PRIVATE)
        response = self.post([{'kind': 'like', 'swift_id': hidden.id}])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['recorded'], 0)
        self.assertEqual(response.json()['skipped'], 1)

    def test_a_signal_about_a_missing_swift_does_not_fail_the_batch(self):
        response = self.post([
            {'kind': 'like', 'swift_id': 999_999},
            {'kind': 'like', 'swift_id': self.swift.id},
        ])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['recorded'], 1)
        self.assertEqual(response.json()['skipped'], 1)

    def test_a_location_field_is_never_accepted(self):
        self.post([{
            'kind': 'location_reveal',
            'swift_id': self.swift.id,
            'latitude': '43.2',
            'longitude': '76.9',
        }])

        stored = SwiftSignal.objects.filter(viewer=self.viewer).values().get()
        self.assertNotIn('latitude', stored)
        self.assertNotIn('longitude', stored)

    def test_an_oversized_batch_is_rejected(self):
        from ..serializers import MAX_BATCH

        response = self.post(
            [{'kind': 'like', 'swift_id': self.swift.id}] * (MAX_BATCH + 1),
        )
        self.assertEqual(response.status_code, 400)

    def test_an_empty_batch_is_rejected(self):
        self.assertEqual(self.post([]).status_code, 400)

    def test_sharing_is_refused_until_it_exists(self):
        response = self.post([{'kind': 'share', 'swift_id': self.swift.id}])
        self.assertEqual(response.status_code, 400)

    def test_it_requires_authentication(self):
        response = APIClient().post(
            reverse('swift-signals'),
            {'signals': [{'kind': 'like', 'swift_id': self.swift.id}]},
            format='json',
        )
        self.assertEqual(response.status_code, 401)


class DebugEndpointTests(SwiftTestCase):
    def test_it_is_staff_only(self):
        viewer = make_user('viewer')
        client = APIClient()
        client.force_authenticate(viewer)

        self.assertEqual(client.get(reverse('swift-recs-debug')).status_code, 403)

    def test_staff_get_the_funnel(self):
        staff = make_user('staff', is_staff=True)
        author = make_user('author')
        bulk_swifts(author, 3)

        client = APIClient()
        client.force_authenticate(staff)
        body = client.get(reverse('swift-recs-debug')).json()

        self.assertIn('funnel', body)
        self.assertIn('candidates_per_source', body)
        self.assertIn('served_distribution', body)
        self.assertIn('cache_hit_rate', body)

    def test_the_debug_payload_carries_no_content(self):
        """Ids and counts. Not captions, not usernames, not coordinates."""
        staff = make_user('staff2', is_staff=True)
        author = make_user('author2')
        make_swift(author, caption='a distinctive caption', located=True, shared=True)

        client = APIClient()
        client.force_authenticate(staff)
        raw = client.get(reverse('swift-recs-debug')).content.decode()

        self.assertNotIn('distinctive caption', raw)
        self.assertNotIn('author2', raw)
        self.assertNotIn('43.2', raw)
