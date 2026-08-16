"""The map check-in gate, which is what App Review asked for under 5.1.2(i).

The rule these all circle is one sentence: *nothing* except a deliberate
check-in puts a user on somebody else's map, and it stops doing so on its own.
The cases below are the ways that could go wrong — a permission mistaken for
consent, a default that leaks, a phone that renews itself, a side channel that
publishes a position the map does not.
"""
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from encounters.models import Encounter
from friends.models import FriendRequest
from interests.models import Interest
from notifications.models import Notification
from users.models import User


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


def check_in(user):
    """Put `user` on the map the way the endpoint would, for other tests."""
    user.save(update_fields=user.apply_check_in())
    return user


class CheckInDefaultTests(TestCase):
    """Off is the state you get for free, from every direction."""

    def test_new_account_is_not_checked_in(self):
        user = make_user('fresh')
        self.assertFalse(user.is_checked_in)
        self.assertIsNone(user.checked_in_at)
        self.assertFalse(user.is_on_map())

    def test_live_location_sharing_is_not_a_check_in(self):
        """The distinction the rejection was about.

        `location_sharing` says how precisely a *published* position is stated.
        It has never meant "publish it", and the default value of Live must not
        be readable as consent to appear.
        """
        user = make_user('sharer', location_sharing=User.LocationSharing.LIVE)
        self.assertFalse(user.is_on_map())

    def test_reporting_a_position_does_not_check_anyone_in(self):
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
        self.assertFalse(user.is_checked_in)
        self.assertFalse(user.is_on_map())


class CheckInVisibilityTests(TestCase):
    """Who comes back from /api/proximity/nearby/, and on what terms."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me', latitude='10.0', longitude='10.0')
        self.other = make_user('other', latitude='10.0005', longitude='10.0005')
        # Strangers only find each other through a shared interest.
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.other.interests.add(interest)
        check_in(self.me)
        self.client.force_authenticate(self.me)

    def ids(self):
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        return {entry['id'] for entry in response.json()}

    def test_checked_out_user_is_absent(self):
        self.assertNotIn(self.other.pk, self.ids())

    def test_checked_in_user_is_present(self):
        check_in(self.other)
        self.assertIn(self.other.pk, self.ids())

    def test_check_in_survives_until_it_is_taken_back(self):
        """A check-in has no clock on it — only a check-out ends it.

        The property the whole design now rests on, and the one most likely to
        be broken by someone reintroducing an expiry: closing the app, locking
        the phone and coming back days later must all leave you exactly as
        visible as you were, because the flag lives on the row and not in a
        session.
        """
        check_in(self.other)
        self.assertIn(self.other.pk, self.ids())

        # Nothing in between — no refresh, no report, no touch of any kind.
        self.assertIn(self.other.pk, self.ids())

        self.other.save(update_fields=self.other.apply_check_out())
        self.assertNotIn(self.other.pk, self.ids())

    def test_friendship_does_not_override_a_check_out(self):
        """Friends are exempt from the distance limits, not from consent."""
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.assertNotIn(self.other.pk, self.ids())
        check_in(self.other)
        self.assertIn(self.other.pk, self.ids())

    def test_checked_out_viewer_can_still_browse(self):
        """Deciding not to be seen is not a decision to be blind.

        Apple's requirement is about being displayed, not about looking, and
        removing the map for anyone who has not checked in would be a UX
        change nobody asked for.
        """
        check_in(self.other)
        self.me.save(update_fields=self.me.apply_check_out())
        self.assertIn(self.other.pk, self.ids())


class CheckInMapEndpointTests(TransactionTestCase):
    """The composite /api/map/nearby/ must not be a second, laxer door.

    `TransactionTestCase` rather than `TestCase`: this view fans its three
    sub-reads out across a thread pool, and SQLite refuses concurrent access to
    tables held inside the single open transaction `TestCase` wraps each test
    in. Nothing to do with the code under test — production is Postgres — but
    the test has to commit to be able to observe it.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me', latitude='10.0', longitude='10.0')
        self.other = make_user('other', latitude='10.0005', longitude='10.0005')
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.other.interests.add(interest)
        check_in(self.me)
        self.client.force_authenticate(self.me)

    def map_user_ids(self):
        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 200)
        return {u['id'] for u in response.json()['users']}

    def test_checked_out_user_is_absent_from_the_map_payload(self):
        self.assertNotIn(self.other.pk, self.map_user_ids())
        check_in(self.other)
        self.assertIn(self.other.pk, self.map_user_ids())


class CheckInEndpointTests(TestCase):
    """The only route that writes the flag."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def test_check_in_publishes_and_stores_the_position(self):
        response = self.client.post(
            '/api/proximity/check-in/',
            {'latitude': '10.0', 'longitude': '20.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_checked_in'])
        self.assertIsNotNone(response.json()['checked_in_at'])

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        self.assertEqual(float(self.me.latitude), 10.0)

    def test_check_in_requires_a_position(self):
        response = self.client.post('/api/proximity/check-in/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in)

    def test_a_duration_cannot_be_asked_for(self):
        """There is one kind of check-in, and the consent screen describes it.

        A client sending `duration_minutes` is either an old build or a
        modified one. Either way it gets the only check-in there is — an
        indefinite one — rather than a quietly shorter promise the user was
        never shown.
        """
        response = self.client.post(
            '/api/proximity/check-in/',
            {'latitude': '1.0', 'longitude': '1.0', 'duration_minutes': 15},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())

    def test_check_out_is_immediate_and_idempotent(self):
        self.client.post(
            '/api/proximity/check-in/',
            {'latitude': '1.0', 'longitude': '1.0'},
            format='json',
        )
        for _ in range(2):
            response = self.client.delete('/api/proximity/check-in/')
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['is_checked_in'])
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        self.assertIsNone(self.me.checked_in_at)

    def test_get_reports_the_state(self):
        response = self.client.get('/api/proximity/check-in/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_checked_in'])

        check_in(self.me)
        response = self.client.get('/api/proximity/check-in/')
        self.assertTrue(response.json()['is_checked_in'])
        self.assertIsNotNone(response.json()['checked_in_at'])

    def test_checking_in_twice_does_not_stack_anything(self):
        """Pressing it again is a no-op, not a second something.

        Worth pinning now that there is no expiry: a second check-in has
        nothing to extend, so the only thing it can do is move `checked_in_at`,
        and it must not leave the user in a state a single check-out cannot
        undo.
        """
        for _ in range(2):
            self.client.post(
                '/api/proximity/check-in/',
                {'latitude': '1.0', 'longitude': '1.0'},
                format='json',
            )
        self.client.delete('/api/proximity/check-in/')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())

    def test_paused_sharing_refuses_a_check_in(self):
        self.me.location_sharing = User.LocationSharing.PAUSED
        self.me.save(update_fields=['location_sharing'])
        response = self.client.post(
            '/api/proximity/check-in/',
            {'latitude': '1.0', 'longitude': '1.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in)

    def test_check_in_cannot_be_set_through_the_privacy_endpoint(self):
        """No settings screen may switch this on — only the map does.

        The one that matters most under an indefinite check-in: a writable flag
        on a settings payload is a standing "always share" preference, which is
        the thing the guideline names outright.
        """
        response = self.client.patch(
            '/api/users/privacy/', {'is_checked_in': True}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_checked_in'])
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in)


class PauseEndsVisibilityTests(TestCase):
    def test_pausing_checks_out_and_forgets_the_position(self):
        me = make_user('me', latitude='10.0', longitude='10.0')
        check_in(me)
        client = APIClient()
        client.force_authenticate(me)

        response = client.patch(
            '/api/users/privacy/', {'location_sharing': 'paused'}, format='json',
        )
        self.assertEqual(response.status_code, 200)

        me.refresh_from_db()
        self.assertFalse(me.is_checked_in)
        self.assertIsNone(me.checked_in_at)
        self.assertIsNone(me.latitude)
        self.assertIsNone(me.longitude)


class CheckInSideChannelTests(TestCase):
    """Everything else that says where somebody is standing."""

    def setUp(self):
        self.mover = make_user('mover', latitude='10.0', longitude='10.0')
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

    def test_no_encounter_is_logged_for_a_checked_out_reporter(self):
        check_in(self.resident)
        self.report()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_encounter_is_logged_once_both_are_checked_in(self):
        check_in(self.resident)
        check_in(self.mover)
        self.report()
        self.assertEqual(Encounter.objects.count(), 1)

    def test_checked_out_user_is_not_announced_as_nearby(self):
        """The push that says "someone is near you" is a publication too."""
        check_in(self.mover)
        self.report()
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.resident,
                notification_type=Notification.Type.NEARBY_USER,
            ).exists(),
            'a checked-out user was told about someone, or announced to them',
        )

    def test_motion_is_not_recorded_while_checked_out(self):
        self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0', 'speed': 12.0},
            format='json',
        )
        self.mover.refresh_from_db()
        self.assertIsNone(self.mover.motion_updated_at)

        check_in(self.mover)
        self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0', 'speed': 12.0},
            format='json',
        )
        self.mover.refresh_from_db()
        self.assertEqual(self.mover.motion_class, User.MotionClass.FAST)
