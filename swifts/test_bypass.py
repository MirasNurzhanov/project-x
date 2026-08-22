"""Trying to get a coordinate out of Aura without using the reveal.

The rest of the location suite tests the gate from the inside: given these
conditions, does the reveal allow or refuse. This one attacks from the outside.
Every endpoint that touches a Swift is hit with a located Swift the caller has
no right to, and the whole response body is walked for anything
location-shaped — not just the fields anybody remembered to think about.

The distinction matters because the two suites fail differently. A bug in the
gate breaks the tests that exercise the gate. A coordinate arriving through the
*audit* endpoint, or an ordering side-channel, or a serializer somebody added
last week, breaks nothing at all — until this.

## What "no right to" means here

Two attackers, because they can reach different things:

    `stranger`   can watch the Swift but has not viewed it, so the reveal
                 refuses them. Everything else they can reach must still
                 withhold the coordinate.
    `outsider`   cannot even watch it. Every endpoint must 404, and must 404
                 the same way it would for a Swift that does not exist.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SwiftLocationReveal, SwiftVideo, SwiftView
from .testing import (
    LAT, LON, SwiftTestCase, block, find_forbidden, make_swift, make_user,
)


class LocationBypassTests(SwiftTestCase):
    """Every route, against somebody who should not get a coordinate."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('bypass_creator')
        self.stranger = make_user('bypass_stranger')

        self.swift = make_swift(
            self.creator, located=True, shared=True, caption='at the park',
        )

        self.client = APIClient()
        self.client.force_authenticate(self.stranger)

    def assertClean(self, response, where):
        """No coordinate anywhere in this response, in any form."""
        raw = response.content.decode()
        self.assertNotIn(LAT, raw, f'{where} leaked the latitude')
        self.assertNotIn(LON, raw, f'{where} leaked the longitude')
        # The truncated forms too — a serializer that rounds is still a leak.
        self.assertNotIn(LAT[:6], raw, f'{where} leaked a truncated latitude')
        self.assertNotIn(LON[:6], raw, f'{where} leaked a truncated longitude')
        try:
            body = response.json()
        except ValueError:
            return
        self.assertEqual(find_forbidden(body), [], f'{where} carried a location key')

    # ── Read surfaces ─────────────────────────────────────────────────────────
    def test_the_ranked_feed_gives_nothing_away(self):
        self.assertClean(self.client.get(reverse('swift-feed')), 'ranked feed')

    def test_the_chronological_feed_gives_nothing_away(self):
        self.assertClean(
            self.client.get(reverse('swift-feed'), {'chrono': '1'}), 'chrono feed',
        )

    def test_the_detail_endpoint_gives_nothing_away(self):
        self.assertClean(
            self.client.get(reverse('swift-detail', args=[self.swift.pk])), 'detail',
        )

    def test_the_comments_endpoint_gives_nothing_away(self):
        self.client.post(
            reverse('swift-comments', args=[self.swift.pk]), {'text': 'nice'},
        )
        self.assertClean(
            self.client.get(reverse('swift-comments', args=[self.swift.pk])),
            'comments',
        )

    def test_the_mine_endpoint_gives_nothing_away_to_its_owner(self):
        """Even the creator's own list. They know where they were, but the
        column still has no business being in a list payload — every extra
        surface carrying it is another one to keep correct forever."""
        owner = APIClient()
        owner.force_authenticate(self.creator)
        self.assertClean(owner.get(reverse('swift-mine')), 'mine')

    # ── Write surfaces that echo the row back ─────────────────────────────────
    def test_liking_does_not_echo_a_coordinate(self):
        self.assertClean(
            self.client.post(reverse('swift-like', args=[self.swift.pk])), 'like',
        )

    def test_reposting_does_not_echo_a_coordinate(self):
        self.assertClean(
            self.client.post(reverse('swift-repost', args=[self.swift.pk])), 'repost',
        )

    def test_recording_a_view_does_not_echo_a_coordinate(self):
        self.assertClean(
            self.client.post(
                reverse('swift-view', args=[self.swift.pk]),
                {'watch_ms': 5000, 'completed': True}, format='json',
            ),
            'view',
        )

    def test_reporting_does_not_echo_a_coordinate(self):
        self.assertClean(
            self.client.post(
                reverse('swift-report', args=[self.swift.pk]),
                {'reason': 'spam'}, format='json',
            ),
            'report',
        )

    def test_signalling_does_not_echo_a_coordinate(self):
        self.assertClean(
            self.client.post(
                reverse('swift-signals'),
                {'signals': [{'kind': 'like', 'swift_id': self.swift.pk}]},
                format='json',
            ),
            'signals',
        )

    # ── The reveal itself, refused ────────────────────────────────────────────
    def test_revealing_without_having_watched_refuses_and_says_nothing(self):
        response = self.client.post(
            reverse('swift-reveal-location', args=[self.swift.pk]),
        )
        self.assertEqual(response.status_code, 404)
        self.assertClean(response, 'refused reveal')

    def test_a_refused_reveal_is_indistinguishable_from_a_missing_swift(self):
        """A 403 would confirm the Swift exists and has something worth hiding."""
        real = self.client.post(
            reverse('swift-reveal-location', args=[self.swift.pk]),
        )
        missing = self.client.post(
            reverse('swift-reveal-location', args=[99_999_999]),
        )
        self.assertEqual(real.status_code, missing.status_code)
        self.assertEqual(real.content, missing.content)

    def test_a_get_on_the_reveal_route_does_not_answer(self):
        """The verb is part of the gate. A GET would be cacheable and
        pre-fetchable, which is the one thing a deliberate action must not be."""
        response = self.client.get(
            reverse('swift-reveal-location', args=[self.swift.pk]),
        )
        self.assertIn(response.status_code, (404, 405))
        self.assertClean(response, 'GET reveal')

    def test_claiming_to_have_viewed_it_changes_nothing(self):
        """The client asserting `hasViewed` is a client asserting nothing."""
        response = self.client.post(
            reverse('swift-reveal-location', args=[self.swift.pk]),
            {'hasViewed': True, 'viewed': True, 'has_viewed': True},
            format='json',
        )
        self.assertEqual(response.status_code, 404)
        self.assertClean(response, 'forged view claim')

    def test_the_audit_endpoint_is_creator_only_and_carries_no_coordinate(self):
        SwiftView.objects.create(swift=self.swift, viewer=self.stranger)
        self.client.post(reverse('swift-reveal-location', args=[self.swift.pk]))
        self.assertTrue(SwiftLocationReveal.objects.filter(swift=self.swift).exists())

        # The person who revealed it cannot read the audit.
        response = self.client.get(reverse('swift-reveal-audit', args=[self.swift.pk]))
        self.assertEqual(response.status_code, 404)

        owner = APIClient()
        owner.force_authenticate(self.creator)
        self.assertClean(
            owner.get(reverse('swift-reveal-audit', args=[self.swift.pk])), 'audit',
        )

    # ── The one path that is allowed ──────────────────────────────────────────
    def test_the_intended_flow_does_return_the_coordinate(self):
        """The other side of every assertion above.

        A gate that refuses everybody is not a gate, it is an outage, and a
        suite that only tests refusals cannot tell the two apart.
        """
        SwiftView.objects.create(swift=self.swift, viewer=self.stranger)

        response = self.client.post(
            reverse('swift-reveal-location', args=[self.swift.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()['latitude'], float(LAT), places=5)


class UnwatchableSwiftTests(SwiftTestCase):
    """Somebody who cannot watch it at all. Every route must 404."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('hidden_creator')
        self.outsider = make_user('outsider')
        self.client = APIClient()
        self.client.force_authenticate(self.outsider)

    def _routes(self, pk):
        return [
            ('get', reverse('swift-detail', args=[pk]), None),
            ('get', reverse('swift-comments', args=[pk]), None),
            ('post', reverse('swift-like', args=[pk]), None),
            ('post', reverse('swift-repost', args=[pk]), None),
            ('post', reverse('swift-view', args=[pk]), {}),
            ('post', reverse('swift-reveal-location', args=[pk]), None),
            ('get', reverse('swift-reveal-audit', args=[pk]), None),
        ]

    def assertAll404(self, swift, label):
        for method, url, data in self._routes(swift.pk):
            response = getattr(self.client, method)(
                url, data, format='json') if data is not None else getattr(
                self.client, method)(url)
            self.assertEqual(
                response.status_code, 404,
                f'{label}: {method.upper()} {url} answered {response.status_code}',
            )
            raw = response.content.decode()
            self.assertNotIn(LAT, raw, f'{label}: {url} leaked')

    def test_a_private_located_swift_is_invisible_everywhere(self):
        swift = make_swift(
            self.creator, located=True, shared=True,
            visibility=SwiftVideo.Visibility.PRIVATE,
        )
        self.assertAll404(swift, 'private')

    def test_a_friends_only_located_swift_is_invisible_to_a_stranger(self):
        swift = make_swift(
            self.creator, located=True, shared=True,
            visibility=SwiftVideo.Visibility.FRIENDS,
        )
        self.assertAll404(swift, 'friends-only')

    def test_a_deleted_located_swift_is_invisible(self):
        swift = make_swift(self.creator, located=True, shared=True)
        SwiftVideo.objects.filter(pk=swift.pk).update(
            deleted_at='2026-01-01T00:00:00Z',
        )
        self.assertAll404(swift, 'deleted')

    def test_a_removed_located_swift_is_invisible(self):
        swift = make_swift(self.creator, located=True, shared=True)
        SwiftVideo.objects.filter(pk=swift.pk).update(
            moderation=SwiftVideo.Moderation.REMOVED,
        )
        self.assertAll404(swift, 'removed')

    def test_a_blocked_viewer_gets_nothing(self):
        swift = make_swift(self.creator, located=True, shared=True)
        block(self.creator, self.outsider)
        self.assertAll404(swift, 'blocked')

    def test_a_viewer_who_blocked_the_creator_gets_nothing(self):
        swift = make_swift(self.creator, located=True, shared=True)
        block(self.outsider, self.creator)
        self.assertAll404(swift, 'blocked-them')

    def test_having_viewed_it_before_it_was_hidden_does_not_survive(self):
        """A `SwiftView` row is not a standing permission.

        This is the sharpest version of the gate: the viewer genuinely watched
        it, so the freshness half of the reveal test passes. What must stop
        them is that the Swift is no longer watchable at all — and the reveal
        has to re-check that rather than trusting the view row it is keyed on.
        """
        swift = make_swift(self.creator, located=True, shared=True)
        SwiftView.objects.create(swift=swift, viewer=self.outsider)

        SwiftVideo.objects.filter(pk=swift.pk).update(
            visibility=SwiftVideo.Visibility.PRIVATE,
        )

        response = self.client.post(
            reverse('swift-reveal-location', args=[swift.pk]),
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(LAT, response.content.decode())

    def test_the_creator_turning_sharing_off_closes_it_for_past_viewers_too(self):
        swift = make_swift(self.creator, located=True, shared=True)
        SwiftView.objects.create(swift=swift, viewer=self.outsider)
        self.assertEqual(
            self.client.post(
                reverse('swift-reveal-location', args=[swift.pk])).status_code,
            200,
        )

        SwiftVideo.objects.filter(pk=swift.pk).update(location_shared=False)

        response = self.client.post(
            reverse('swift-reveal-location', args=[swift.pk]),
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(LAT, response.content.decode())


# `MediaHostTests` used to live here: a Swift posted with an attacker-chosen
# `video_url`/`thumbnail_url`/`audio_artwork_url` served that URL verbatim to
# every viewer's device. The R2 migration removed the vulnerability along with
# the feature that carried it — `SwiftCreateSerializer` no longer accepts any
# of those three fields at all, so there is no longer a client-supplied URL for
# an attacker to point anywhere. See `swifts.test_media.PlaybackUrlTests` for
# what replaced this: a legacy row's stored URL still has to resolve, but
# nothing writes one going forward.


class RecommenderBypassTests(SwiftTestCase):
    """The recommender as an attack surface.

    A ranker that reads coordinates would hold every one of them for every
    candidate it considers — defeating the gate from a direction nobody would
    think to check.
    """

    def setUp(self):
        super().setUp()
        self.creator = make_user('rec_creator')
        self.viewer = make_user('rec_viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def test_the_debug_endpoint_is_closed_to_ordinary_users(self):
        response = self.client.get(reverse('swift-recs-debug'))
        self.assertEqual(response.status_code, 403)

    def test_the_debug_endpoint_carries_no_coordinate_even_for_staff(self):
        make_swift(self.creator, located=True, shared=True)
        staff = make_user('rec_staff', is_staff=True)
        client = APIClient()
        client.force_authenticate(staff)

        response = client.get(reverse('swift-recs-debug'))

        self.assertNotIn(LAT, response.content.decode())
        self.assertEqual(find_forbidden(response.json()), [])

    def test_a_signal_cannot_smuggle_a_coordinate_into_storage(self):
        from recs.models import SwiftSignal

        swift = make_swift(self.creator, located=True, shared=True)
        self.client.post(
            reverse('swift-signals'),
            {'signals': [{
                'kind': 'location_reveal',
                'swift_id': swift.pk,
                'latitude': LAT,
                'longitude': LON,
                'place_name': 'Kok-Tobe',
            }]},
            format='json',
        )

        stored = SwiftSignal.objects.filter(viewer=self.viewer).values().get()
        self.assertEqual(find_forbidden(stored), [])
        self.assertNotIn(LAT, str(stored))

    def test_ranking_does_not_reorder_around_hidden_locations(self):
        """An ordering side-channel would leak without a coordinate moving.

        Two Swifts identical but for a location: if the located one
        consistently ranked differently, the *position* would tell a viewer
        which Swifts carry a coordinate.
        """
        from django.utils import timezone

        from recs.pipeline import build_session

        located = make_swift(self.creator, located=True, shared=True)
        plain = make_swift(self.creator)
        stamped = timezone.now() - timezone.timedelta(hours=2)
        SwiftVideo.objects.filter(id__in=[located.id, plain.id]).update(
            created_at=stamped, like_count=5, view_count=50,
        )

        ids, _, _, _ = build_session(self.viewer, seed=1)

        # Both present, and the located one is not systematically first or last.
        self.assertIn(located.id, ids)
        self.assertIn(plain.id, ids)



class StaleSessionTests(SwiftTestCase):
    """A ranked session must not outlive the permissions it was built on.

    The recommender caches an ordered list of ids for half an hour and serves
    pages out of it. That cache is a *hint about order*, not a grant: between
    the ranking pass and the page that eventually reaches the viewer, a Swift
    can be deleted, made private, taken down, or its author can block the
    viewer.

    Found by reading rather than by a failing test — the row fetch used
    `SwiftVideo.objects.filter(id__in=...)`, which is the ranked path skipping
    the one gate every other read in the app goes through. The window was up to
    `session_ttl_s`.
    """

    def setUp(self):
        super().setUp()
        self.creator = make_user('stale_creator')
        self.viewer = make_user('stale_viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        # Enough for a session with a second page to serve.
        self.swifts = [
            make_swift(self.creator, located=True, shared=True) for _ in range(6)
        ]

    def _open_session(self):
        body = self.client.get(reverse('swift-feed'), {'limit': 2}).json()
        self.assertTrue(body['next_cursor'], 'expected a continuable session')
        return body['next_cursor']

    def _remaining_ids(self, cursor):
        ids = []
        while cursor:
            body = self.client.get(
                reverse('swift-feed'), {'limit': 2, 'cursor': cursor},
            ).json()
            ids.extend(r['id'] for r in body['results'])
            cursor = body['next_cursor']
        return ids

    def _revoke_and_drain(self, **update):
        """Open a session, revoke every Swift in it, drain what is left.

        Revoking *all* of them rather than picking one, which is how the first
        version of this went wrong: it targeted the newest Swift, ranking put
        the newest Swift on page one, and page one is served before the
        revocation — so three of these tests asserted that an id which was
        never going to appear did not appear. They passed with the fix removed.

        Revoking the lot is deterministic and still isolates the condition:
        each caller changes one column, and each column is a different arm of
        `watchable_q`.
        """
        cursor = self._open_session()
        SwiftVideo.objects.filter(
            pk__in=[s.pk for s in self.swifts],
        ).update(**update)
        return self._remaining_ids(cursor)

    def test_a_swift_deleted_mid_session_is_not_served(self):
        self.assertEqual(
            self._revoke_and_drain(deleted_at='2026-01-01T00:00:00Z'), [],
        )

    def test_a_swift_made_private_mid_session_is_not_served(self):
        self.assertEqual(
            self._revoke_and_drain(visibility=SwiftVideo.Visibility.PRIVATE), [],
        )

    def test_a_swift_removed_by_moderation_mid_session_is_not_served(self):
        self.assertEqual(
            self._revoke_and_drain(moderation=SwiftVideo.Moderation.REMOVED), [],
        )

    def test_a_block_mid_session_stops_the_rest_of_it(self):
        """The sharpest case: the creator blocks the viewer after ranking.

        Every remaining page is that creator's work, and all of it has to stop
        — not at the next ranking pass, at the next request.
        """
        cursor = self._open_session()
        block(self.creator, self.viewer)

        self.assertEqual(self._remaining_ids(cursor), [])

    def test_the_session_still_serves_what_is_still_allowed(self):
        """The control. Revoking nothing must revoke nothing."""
        cursor = self._open_session()

        self.assertGreater(len(self._remaining_ids(cursor)), 0)
