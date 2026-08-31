"""Who is on the map, and on what terms.

The rule these circle is one sentence: you are on the map when you are not
ghosted *and* we hold a position fresh enough to stand behind. Both halves, and
the second is not decoration — a setting with no position behind it publishes
nothing, which is what a cold install and a phone in a tunnel have in common.

The rule used to be a different sentence: nothing but an explicit per-launch
choice could publish anybody, and everybody started off. That was the shape of
an App Review remedy under 5.1.2(i), and it is deliberately gone — sharing
follows the OS permission now and persists across launches. Tests that asserted
the old default were rewritten to assert the new one rather than deleted, so
the file still says, from every direction, exactly who is visible and why.

`ghost_mode` is the only axis left. Precision is no longer one: friends see the
precise point and everybody else a ~500m cell, which
`proximity.test_compliance.PrecisionFollowsTheRelationshipTests` owns.
"""
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from encounters.models import Encounter
from friends.models import FriendRequest
from interests.models import Interest
from notifications.models import Notification
from users.models import User
from users.testing import share_location


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


class SharingDefaultTests(TestCase):
    """What you get for free, from every direction."""

    def test_a_new_account_is_not_ghosted_and_not_yet_on_the_map(self):
        """Both halves of the rule, on the row that has neither disproved.

        Willing to be seen, and invisible anyway — there is no position. This
        is the assertion that would catch the freshness guard being loosened
        into "recently saved", which a default of not-ghosted makes reachable
        for the first time.
        """
        user = make_user('fresh')
        self.assertFalse(user.ghost_mode)
        self.assertIsNone(user.latitude)
        self.assertFalse(user.is_on_map())

    def test_reporting_a_position_is_what_puts_someone_on_the_map(self):
        """The write is no longer refused, and that is the change.

        It used to 403 unless the user had checked in, so that a permission
        could never be mistaken for consent. Consent is the OS permission now,
        and the position it produces is what makes somebody visible.
        """
        user = make_user('reporter')
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(float(user.latitude), 10.0)
        self.assertTrue(user.is_on_map())

    def test_a_ghost_reports_but_is_still_off_the_map(self):
        """The other order: the position lands, the visibility does not.

        A ghost keeps reporting because their own clubs and region resolve from
        that coordinate. What ghosting removes is the publication, not the row.
        """
        user = make_user('ghost-reporter')
        user.ghost_mode = True
        user.save(update_fields=['ghost_mode'])
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(float(user.latitude), 10.0)
        self.assertFalse(user.is_on_map())

    def test_sharing_lets_the_position_through(self):
        user = share_location(make_user('sharer'))
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(float(user.latitude), 10.0)


class MapVisibilityTests(TestCase):
    """Who comes back from /api/proximity/nearby/, and on what terms."""

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(
            make_user('me', latitude='10.0', longitude='10.0'))
        self.other = make_user('other', latitude='10.0005', longitude='10.0005')
        # Strangers only find each other through a shared interest.
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.other.interests.add(interest)
        self.client.force_authenticate(self.me)

    def ids(self):
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        return {entry['id'] for entry in response.json()}

    def ghost(self, user):
        user.ghost_mode = True
        user.save(update_fields=['ghost_mode'])

    def test_a_ghost_is_absent(self):
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())
        self.ghost(self.other)
        self.assertNotIn(self.other.pk, self.ids())

    def test_a_sharing_user_is_present(self):
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())

    def test_visibility_lasts_until_it_is_taken_back(self):
        """There is no clock on it — only Ghost Mode ends it.

        The property the design rests on, and the one most likely to be broken
        by someone reintroducing an expiry: closing the app, locking the phone
        and coming back days later must all leave you exactly as visible as you
        were, because the setting lives on the row and not in a session. That
        used to be false by design — a cold launch ended the session — so this
        is now the assertion carrying the change.
        """
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())
        # Nothing in between — no refresh, no report, no touch of any kind.
        self.assertIn(self.other.pk, self.ids())

        self.ghost(self.other)
        self.assertNotIn(self.other.pk, self.ids())

    def test_friendship_does_not_override_ghosting(self):
        """Friends are exempt from the distance limits, not from the off
        switch. Precision is the thing friendship changes — see
        `proximity.test_compliance.PrecisionFollowsTheRelationshipTests`."""
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other,
            status=FriendRequest.Status.ACCEPTED,
        )
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())

        self.ghost(self.other)
        self.assertNotIn(self.other.pk, self.ids())


class MapEndpointTests(TransactionTestCase):
    """The composite /api/map/nearby/ must not be a second, laxer door.

    `TransactionTestCase` rather than `TestCase`: this view fans its three
    sub-reads out across a thread pool, and SQLite refuses concurrent access to
    tables held inside the single open transaction `TestCase` wraps each test
    in. Nothing to do with the code under test — production is Postgres — but
    the test has to commit to be able to observe it.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(
            make_user('me', latitude='10.0', longitude='10.0'))
        self.other = make_user('other', latitude='10.0005', longitude='10.0005')
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.other.interests.add(interest)
        self.client.force_authenticate(self.me)

    def map_user_ids(self):
        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 200)
        return {u['id'] for u in response.json()['users']}

    def test_a_ghost_is_absent_from_the_map_payload(self):
        share_location(self.other)
        self.assertIn(self.other.pk, self.map_user_ids())

        self.other.ghost_mode = True
        self.other.save(update_fields=['ghost_mode'])
        self.assertNotIn(self.other.pk, self.map_user_ids())


class GhostTransitionTests(TestCase):
    """Turning Ghost Mode on and off, and what each move drags with it.

    This class used to be `SharingTransitionTests` and it asserted a teardown:
    pausing deleted the stored position, the motion trail and the session, and
    resuming started from nothing. Ghost Mode deliberately keeps all of it — the
    position still answers the ghost's own clubs and region — so what is
    asserted now is that the *publication* stops and the row does not.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(
            make_user('me', latitude='10.0', longitude='10.0'))
        self.client.force_authenticate(self.me)

    def patch(self, ghost):
        return self.client.patch(
            '/api/users/privacy/', {'ghost_mode': ghost}, format='json',
        )

    def test_ghosting_takes_you_off_the_map_without_forgetting_you(self):
        response = self.patch(True)
        self.assertEqual(response.status_code, 200)

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        # Kept, deliberately: clubs and region resolve from this.
        self.assertEqual(float(self.me.latitude), 10.0)
        self.assertIsNotNone(self.me.location_updated_at)

    def test_ghosting_leaves_motion_unpublished_rather_than_erased(self):
        """A speed trail is drawn from a published position and must not
        outlive it — but `motion_for` is gated on the same visibility, so
        nothing has to be wiped for that to hold."""
        from users import visibility

        self.me.motion_class = User.MotionClass.FAST
        self.me.motion_updated_at = self.me.created_at
        self.me.save(update_fields=['motion_class', 'motion_updated_at'])

        self.patch(True)
        self.me.refresh_from_db()

        watcher = share_location(make_user('watcher'))
        FriendRequest.objects.create(
            sender=watcher, receiver=self.me,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.assertIsNone(visibility.motion_for(watcher, self.me))

    def test_unghosting_puts_you_straight_back(self):
        """No re-report needed. The position was never thrown away, so coming
        back is one field and takes effect on the next request."""
        self.patch(True)
        self.patch(False)

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        self.assertEqual(float(self.me.latitude), 10.0)

    def test_the_payload_reports_the_state_back(self):
        self.assertTrue(self.patch(True).json()['ghost_mode'])
        self.assertFalse(self.patch(False).json()['ghost_mode'])

    def test_the_legacy_spelling_still_reads_back(self):
        """Builds predating Ghost Mode branch on `location_sharing`."""
        self.assertEqual(self.patch(True).json()['location_sharing'], 'paused')
        self.assertEqual(self.patch(False).json()['location_sharing'], 'live')


class SideChannelTests(TestCase):
    """Everything else that says where somebody is standing."""

    def setUp(self):
        self.mover = share_location(
            make_user('mover', latitude='10.0', longitude='10.0'))
        self.resident = make_user('resident', latitude='10.0002', longitude='10.0002')
        interest = Interest.objects.create(name='Climbing')
        self.mover.interests.add(interest)
        self.resident.interests.add(interest)
        self.client = APIClient()
        self.client.force_authenticate(self.mover)

    def report(self):
        return self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )

    def test_no_encounter_is_logged_with_a_ghosted_bystander(self):
        self.resident.ghost_mode = True
        self.resident.save(update_fields=['ghost_mode'])
        self.report()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_encounter_is_logged_once_both_are_sharing(self):
        share_location(self.resident)
        self.report()
        self.assertEqual(Encounter.objects.count(), 1)

    def test_a_ghosted_user_is_not_announced_as_nearby(self):
        """The push that says "someone is near you" is a publication too."""
        self.resident.ghost_mode = True
        self.resident.save(update_fields=['ghost_mode'])
        self.report()
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.resident,
                notification_type=Notification.Type.NEARBY_USER,
            ).exists(),
            'a paused user was told about someone, or announced to them',
        )

    def test_a_ghosted_reporter_reaches_none_of_the_side_channels(self):
        """The report is accepted now — a ghost still needs their own position
        for clubs and region — so the guarantee moved from refusing the write
        to refusing everything downstream of it. An encounter is a mutual
        publication, and a ghost is not part of one in either direction."""
        self.mover.ghost_mode = True
        self.mover.save(update_fields=['ghost_mode'])
        share_location(self.resident)

        self.assertEqual(self.report().status_code, 200)
        self.assertEqual(Encounter.objects.count(), 0)
        self.assertFalse(
            Notification.objects.filter(
                notification_type=Notification.Type.NEARBY_USER,
            ).exists()
        )


class GoingLiveEndToEndTests(TransactionTestCase):
    """The exact sequence the app performs when somebody chooses Live.

    Written after a report of "I went Live and the map was empty". The steps
    matter and so does their order: a user comes off Paused with *no stored
    position*, because pausing deleted it, so the privacy PATCH and the first
    location report are two separate round trips with a gap in between.

    `TransactionTestCase` because /api/map/nearby/ fans out across a thread
    pool — see MapEndpointTests.
    """

    def setUp(self):
        self.interest = Interest.objects.create(name='Climbing')
        self.me = make_user('me')
        self.neighbour = make_user('neighbour')
        for user in (self.me, self.neighbour):
            user.interests.add(self.interest)
        self.client = APIClient()
        self.client.force_authenticate(self.me)

    def go_live(self, client, user):
        """Privacy first, then a position — the order the client uses."""
        response = client.patch(
            '/api/users/privacy/', {'ghost_mode': False}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()

    def test_going_live_shows_other_live_users(self):
        their_client = APIClient()
        their_client.force_authenticate(self.neighbour)
        self.go_live(their_client, self.neighbour)
        self.go_live(self.client, self.me)

        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(
            self.neighbour.pk, {u['id'] for u in response.json()['users']},
            'Two users both on Live and standing on the same spot must see '
            'each other.',
        )

    def test_the_map_is_empty_only_because_the_neighbour_is_ghosted(self):
        """The difference between "the query is broken" and "nobody is there".

        This used to describe the state every account was in after
        users/0029, which paused everybody. Nobody is paused by default now, so
        the way to be absent while holding a position is to have chosen it —
        and that is what is pinned here, because an empty map with an
        explanation is a very different bug report from an empty map without
        one.
        """
        self.neighbour.latitude, self.neighbour.longitude = 10, 10
        self.neighbour.ghost_mode = True
        self.neighbour.save(
            update_fields=['latitude', 'longitude', 'ghost_mode'])
        self.go_live(self.client, self.me)

        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['users'], [])

        self.neighbour.ghost_mode = False
        self.neighbour.save(update_fields=['ghost_mode'])
        share_location(self.neighbour)
        response = self.client.get('/api/map/nearby/')
        self.assertIn(self.neighbour.pk, {u['id'] for u in response.json()['users']})

    def test_the_gap_before_the_first_report_is_a_400_the_client_can_act_on(self):
        """What the client sees between the two round trips above.

        400, not an empty list: an empty list is indistinguishable from "nobody
        is around" and the client would draw a blank map. The 400 is what
        services/discovery.ts keys its fallback to the private query on.
        """
        self.client.patch(
            '/api/users/privacy/', {'ghost_mode': False}, format='json',
        )
        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 400)

    def test_the_fallback_query_answers_during_that_gap(self):
        share_location(self.neighbour)
        self.neighbour.latitude, self.neighbour.longitude = 10, 10
        self.neighbour.save(update_fields=['latitude', 'longitude'])

        self.client.patch(
            '/api/users/privacy/', {'ghost_mode': False}, format='json',
        )
        response = self.client.post(
            '/api/map/nearby/query/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(
            self.neighbour.pk, {u['id'] for u in response.json()['users']},
        )
