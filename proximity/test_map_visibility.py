"""Who is on the map, which is what App Review rejected under 5.1.2(i).

The rule these all circle is one sentence: *nothing* except the user's own
choice of Live or Approximate puts them on somebody else's map, and Paused is
where everybody starts. The cases below are the ways that could go wrong — a
permission mistaken for consent, a default that leaks, a side channel that
publishes a position the map does not.

`location_sharing` is the only axis. It briefly sat beside a second
`is_checked_in` flag; once the three mode buttons became the only control the
two were fully determined by each other, so they were collapsed. Anything
asserting the old flag is gone rather than translated — see
users/migrations/0029.
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
    """Off is the state you get for free, from every direction."""

    def test_new_account_is_paused(self):
        user = make_user('fresh')
        self.assertEqual(user.location_sharing, User.LocationSharing.PAUSED)
        self.assertFalse(user.is_on_map())

    def test_reporting_a_position_does_not_put_anyone_on_the_map(self):
        """The distinction the rejection was about.

        Granting the OS permission and having the app report a position is not
        consent to be published, so the reporter refuses outright rather than
        storing something it will not show.
        """
        user = make_user('reporter')
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)
        user.refresh_from_db()
        self.assertIsNone(user.latitude)
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

    def test_paused_user_is_absent(self):
        self.assertNotIn(self.other.pk, self.ids())

    def test_sharing_user_is_present(self):
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())

    def test_approximate_is_still_on_the_map(self):
        """Approximate is a precision, not an off switch — unlike Paused."""
        share_location(self.other, User.LocationSharing.APPROXIMATE)
        self.assertIn(self.other.pk, self.ids())

    def test_visibility_lasts_until_it_is_taken_back(self):
        """There is no clock on it — only choosing Paused ends it.

        The property the design rests on, and the one most likely to be broken
        by someone reintroducing an expiry: closing the app, locking the phone
        and coming back days later must all leave you exactly as visible as you
        were, because the setting lives on the row and not in a session.
        """
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())
        # Nothing in between — no refresh, no report, no touch of any kind.
        self.assertIn(self.other.pk, self.ids())

        self.other.location_sharing = User.LocationSharing.PAUSED
        self.other.save(update_fields=['location_sharing'])
        self.assertNotIn(self.other.pk, self.ids())

    def test_friendship_does_not_override_pausing(self):
        """Friends are exempt from the distance limits, not from consent."""
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.assertNotIn(self.other.pk, self.ids())
        share_location(self.other)
        self.assertIn(self.other.pk, self.ids())


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

    def test_paused_user_is_absent_from_the_map_payload(self):
        self.assertNotIn(self.other.pk, self.map_user_ids())
        share_location(self.other)
        self.assertIn(self.other.pk, self.map_user_ids())


class SharingTransitionTests(TestCase):
    """Moving between the three modes, and what each move drags with it."""

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(
            make_user('me', latitude='10.0', longitude='10.0'))
        self.client.force_authenticate(self.me)

    def patch(self, mode):
        return self.client.patch(
            '/api/users/privacy/', {'location_sharing': mode}, format='json',
        )

    def test_pausing_forgets_the_position(self):
        response = self.patch('paused')
        self.assertEqual(response.status_code, 200)

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        self.assertIsNone(self.me.latitude)
        self.assertIsNone(self.me.longitude)
        self.assertIsNone(self.me.location_updated_at)

    def test_pausing_clears_motion(self):
        """A speed trail is drawn from a published position, so it must not
        outlive the position it belongs to."""
        self.me.motion_class = User.MotionClass.FAST
        self.me.motion_updated_at = self.me.created_at
        self.me.save(update_fields=['motion_class', 'motion_updated_at'])

        self.patch('paused')

        self.me.refresh_from_db()
        self.assertEqual(self.me.motion_class, User.MotionClass.STATIONARY)
        self.assertIsNone(self.me.motion_updated_at)

    def test_resuming_starts_from_no_position(self):
        """Coming back does not restore where you were.

        Pausing deleted it and nothing reconstructs it — the next position on
        file is one the phone reports after the user has chosen to share again.
        """
        self.patch('paused')
        self.patch('live')

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        self.assertIsNone(self.me.latitude)

    def test_switching_to_approximate_destroys_the_precise_position(self):
        self.patch('approximate')

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        # Snapped to the ~500m grid, so no longer the point that was reported.
        self.assertNotEqual(float(self.me.latitude), 10.0)

    def test_the_payload_reports_the_mode_back(self):
        self.assertEqual(self.patch('paused').json()['location_sharing'], 'paused')
        self.assertEqual(self.patch('live').json()['location_sharing'], 'live')


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

    def test_no_encounter_is_logged_with_a_paused_bystander(self):
        self.report()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_encounter_is_logged_once_both_are_sharing(self):
        share_location(self.resident)
        self.report()
        self.assertEqual(Encounter.objects.count(), 1)

    def test_a_paused_user_is_not_announced_as_nearby(self):
        """The push that says "someone is near you" is a publication too."""
        self.report()
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.resident,
                notification_type=Notification.Type.NEARBY_USER,
            ).exists(),
            'a paused user was told about someone, or announced to them',
        )

    def test_a_paused_reporter_cannot_reach_the_side_channels_at_all(self):
        """The reporter is refused before any of this runs."""
        self.mover.location_sharing = User.LocationSharing.PAUSED
        self.mover.save(update_fields=['location_sharing'])
        share_location(self.resident)

        self.assertEqual(self.report().status_code, 403)
        self.assertEqual(Encounter.objects.count(), 0)


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
            '/api/users/privacy/', {'location_sharing': 'live'}, format='json',
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

    def test_the_map_is_empty_only_because_everyone_else_is_paused(self):
        """The state every account is in immediately after users/0029.

        Not a defect: the neighbour has a position on file from before the
        migration and is still not shown, because the migration paused them and
        only they can undo that. Pinned so the difference between "the query is
        broken" and "nobody has opted in yet" stays legible.
        """
        self.neighbour.latitude, self.neighbour.longitude = 10, 10
        self.neighbour.save(update_fields=['latitude', 'longitude'])
        self.go_live(self.client, self.me)

        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['users'], [])

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
            '/api/users/privacy/', {'location_sharing': 'live'}, format='json',
        )
        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 400)

    def test_the_fallback_query_answers_during_that_gap(self):
        share_location(self.neighbour)
        self.neighbour.latitude, self.neighbour.longitude = 10, 10
        self.neighbour.save(update_fields=['latitude', 'longitude'])

        self.client.patch(
            '/api/users/privacy/', {'location_sharing': 'live'}, format='json',
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
