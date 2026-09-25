"""Visibility by relationship tier — App Review 5.1.2(i), second rejection.

The rule, in the order these tests take it:

* Friends see each other whenever Ghost Mode is off: precise, across launches,
  in the background, with no check-in.
* Connected users and strangers see somebody only after that person taps Check
  in during the current session, and only while that session lasts. Mutual: a
  viewer who is not checked in sees friends only.
* Private is friends-only permanently; there is no check-in to make.
  Discoverable only makes the check-in *possible*.
* Nothing that is not a tap checks anybody in — not a location report, not a
  setting, not reading your own profile — and a new launch ends a check-in it
  cannot present the token for.
* The gate is on who may see a position, never on whether one is stored:
  checking out, going Private and ending a stale session leave it in place.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from friends.models import FriendRequest, HiRequest
from interests.models import Interest
from users import visibility
from users.models import User

QUERY_URL = '/api/proximity/nearby/query/'


def make_user(name, **extra):
    user = User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123',
        latitude='10.0', longitude='10.0', **extra,
    )
    user.location_updated_at = timezone.now()
    user.save(update_fields=['location_updated_at'])
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def befriend(a, b):
    FriendRequest.objects.create(sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED)


def connect(a, b):
    HiRequest.objects.create(sender=a, receiver=b, status=HiRequest.Status.ACCEPTED)


class TierTests(TestCase):
    def setUp(self):
        self.interest = Interest.objects.create(name='Climbing')
        self.me = make_user('me')
        self.friend = make_user('friend')
        self.connection = make_user('connection')
        self.stranger = make_user('stranger')
        for user in (self.me, self.friend, self.connection, self.stranger):
            user.interests.add(self.interest)
        befriend(self.me, self.friend)
        connect(self.connection, self.me)

    def seen_by(self, viewer):
        response = client_for(viewer).post(QUERY_URL, {'latitude': '10.0', 'longitude': '10.0'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id']: row for row in response.json()}

    def check_in(self, user):
        response = client_for(user).post('/api/proximity/check-in/')
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        return response.json()

    def test_friends_see_each_other_without_a_check_in(self):
        rows = self.seen_by(self.friend)
        self.assertIn(self.me.pk, rows)
        self.assertEqual(rows[self.me.pk]['location_precision'], 'precise')
        self.assertEqual(float(rows[self.me.pk]['latitude']), 10.0)

    def test_friends_keep_seeing_a_backgrounded_position_for_a_day(self):
        User.objects.filter(pk=self.me.pk).update(location_updated_at=timezone.now() - timedelta(hours=3))
        self.assertIn(self.me.pk, self.seen_by(self.friend))

    def test_nobody_else_sees_a_user_who_has_not_checked_in(self):
        for viewer in (self.connection, self.stranger):
            self.check_in(viewer)
            self.assertNotIn(self.me.pk, self.seen_by(viewer), viewer.username)

    def test_a_check_in_shows_the_user_to_checked_in_non_friends_only(self):
        self.check_in(self.me)
        # Mutual: the non-friends are not checked in, so they see nobody but
        # their own friends.
        self.assertNotIn(self.me.pk, self.seen_by(self.stranger))
        self.assertNotIn(self.me.pk, self.seen_by(self.connection))

        self.check_in(self.stranger)
        self.check_in(self.connection)
        stranger_view = self.seen_by(self.stranger)
        self.assertIn(self.me.pk, stranger_view)
        self.assertEqual(stranger_view[self.me.pk]['location_precision'], 'approximate')
        self.assertIn(self.me.pk, self.seen_by(self.connection))

    def test_a_viewer_who_is_not_checked_in_sees_friends_only(self):
        for other in (self.friend, self.connection, self.stranger):
            self.check_in(other)
        self.assertEqual(set(self.seen_by(self.me)), {self.friend.pk})

    def test_coordinates_for_withholds_an_unchecked_in_non_friend(self):
        self.check_in(self.stranger)
        self.assertEqual(visibility.coordinates_for(self.stranger, self.me), (None, None))
        self.check_in(self.me)
        self.assertIsNotNone(visibility.coordinates_for(self.stranger, self.me)[0])
        # A friend's precise point never needed one.
        self.assertEqual(float(visibility.coordinates_for(self.friend, self.me)[0]), 10.0)

    def test_a_check_in_expires_after_the_cap(self):
        self.check_in(self.me)
        self.check_in(self.stranger)
        self.assertIn(self.me.pk, self.seen_by(self.stranger))
        User.objects.filter(pk=self.me.pk).update(
            checked_in_at=timezone.now() - visibility.check_in_max_age() - timedelta(seconds=1))
        self.assertNotIn(self.me.pk, self.seen_by(self.stranger))
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())

    def test_proximity_surfaces_follow_the_same_tiers(self):
        self.assertEqual(visibility.proximity_visible_ids(self.me, [self.friend.pk, self.stranger.pk]),
                         {self.friend.pk})
        self.check_in(self.me)
        self.assertEqual(visibility.proximity_visible_ids(self.me, [self.friend.pk, self.stranger.pk]),
                         {self.friend.pk})
        self.check_in(self.stranger)
        self.assertEqual(visibility.proximity_visible_ids(self.me, [self.friend.pk, self.stranger.pk]),
                         {self.friend.pk, self.stranger.pk})


class OnlyATapChecksInTests(TestCase):
    def setUp(self):
        self.me = make_user('me')
        self.client = client_for(self.me)

    def test_new_and_migrated_accounts_are_discoverable_and_not_checked_in(self):
        self.assertEqual(self.me.discovery_mode, User.DiscoveryMode.DISCOVERABLE)
        self.assertFalse(self.me.is_checked_in())

    def test_reporting_a_position_does_not_check_in(self):
        self.client.patch('/api/proximity/location/', {'latitude': '10.0', 'longitude': '10.0'}, format='json')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())

    def test_choosing_discoverable_does_not_check_in(self):
        self.client.patch('/api/users/privacy/', {'discovery_mode': 'private'}, format='json')
        self.client.patch('/api/users/privacy/', {'discovery_mode': 'discoverable'}, format='json')
        self.client.patch('/api/users/privacy/', {'ghost_mode': False}, format='json')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())

    def test_the_tap_checks_in_and_only_its_response_carries_the_token(self):
        body = self.client.post('/api/proximity/check-in/').json()
        self.assertTrue(body['checked_in'])
        self.assertTrue(body['session'])
        self.assertIsNotNone(body['check_in_expires_at'])
        me = self.client.get('/api/users/me/').json()
        privacy = self.client.get('/api/users/privacy/').json()
        self.assertTrue(me['checked_in'])
        for payload in (me, privacy):
            self.assertNotIn('check_in_session', payload)
            self.assertNotIn(body['session'], str(payload))

    def test_private_and_ghost_accounts_cannot_check_in(self):
        self.client.patch('/api/users/privacy/', {'discovery_mode': 'private'}, format='json')
        response = self.client.post('/api/proximity/check-in/')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'private')

        self.client.patch('/api/users/privacy/', {'discovery_mode': 'discoverable', 'ghost_mode': True}, format='json')
        response = self.client.post('/api/proximity/check-in/')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ghost_mode')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())


class SessionEndTests(TestCase):
    def setUp(self):
        self.me = make_user('me')
        self.client = client_for(self.me)
        self.session = self.client.post('/api/proximity/check-in/').json()['session']

    def assert_position_kept(self):
        self.me.refresh_from_db()
        self.assertEqual(float(self.me.latitude), 10.0)
        self.assertEqual(float(self.me.longitude), 10.0)
        self.assertIsNotNone(self.me.location_updated_at)

    def end_stale(self, session):
        response = self.client.post('/api/proximity/check-in/end-stale/', {'session': session}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_the_run_that_checked_in_keeps_its_check_in(self):
        self.assertTrue(self.end_stale(self.session)['checked_in'])

    def test_a_new_launch_without_the_token_ends_it_and_keeps_the_position(self):
        self.assertFalse(self.end_stale(None)['checked_in'])
        self.assert_position_kept()
        self.assertFalse(self.me.is_checked_in())

    def test_a_token_from_another_run_ends_it(self):
        other = self.client.post('/api/proximity/check-in/').json()['session']
        self.assertNotEqual(other, self.session)
        self.assertFalse(self.end_stale(self.session)['checked_in'])

    def test_checking_out_ends_it_and_keeps_the_position(self):
        self.assertFalse(self.client.post('/api/proximity/check-out/').json()['checked_in'])
        self.assert_position_kept()

    def test_going_private_or_ghost_ends_it_and_switching_back_does_not_restore_it(self):
        self.client.patch('/api/users/privacy/', {'discovery_mode': 'private'}, format='json')
        self.me.refresh_from_db()
        self.assertIsNone(self.me.check_in_session)
        self.client.patch('/api/users/privacy/', {'discovery_mode': 'discoverable'}, format='json')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())
        self.assert_position_kept()

        self.client.post('/api/proximity/check-in/')
        self.client.patch('/api/users/privacy/', {'ghost_mode': True}, format='json')
        self.client.patch('/api/users/privacy/', {'ghost_mode': False}, format='json')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_checked_in())

    def test_signing_out_ends_it(self):
        self.client.post('/api/users/logout/', {'refresh': 'not-a-token'}, format='json')
        self.me.refresh_from_db()
        self.assertIsNone(self.me.check_in_session)

    @override_settings(MAP_CHECK_IN_MAX_AGE_S=60)
    def test_the_cap_is_configurable(self):
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(seconds=61)):
            self.assertFalse(self.me.is_checked_in())


class ProximityAlertTierTests(TestCase):
    """Nearby alerts and encounters follow the map's tiers: friends
    persistently, everybody else across two check-ins."""

    def setUp(self):
        from encounters.models import Encounter
        from notifications.models import Notification

        self.Encounter, self.Notification = Encounter, Notification
        self.me = make_user('me')
        self.friend = make_user('friend')
        self.stranger = make_user('stranger')
        befriend(self.me, self.friend)
        # Deliberately no shared interest with the friend; the stranger shares one.
        climbing = Interest.objects.create(name='Climbing')
        self.me.interests.add(climbing)
        self.stranger.interests.add(climbing)

    def report(self, user):
        client_for(user).patch('/api/proximity/location/', {'latitude': '10.0', 'longitude': '10.0'}, format='json')

    def alerted(self, recipient, sender):
        return self.Notification.objects.filter(
            recipient=recipient, sender=sender, notification_type=self.Notification.Type.NEARBY_USER).first()

    def met(self, a, b):
        return self.Encounter.objects.filter(
            user_one=min(a, b, key=lambda u: u.pk), user_two=max(a, b, key=lambda u: u.pk)).exists()

    def test_friends_are_alerted_and_met_without_checking_in_or_sharing_interests(self):
        self.report(self.me)
        both = (self.alerted(self.friend, self.me), self.alerted(self.me, self.friend))
        self.assertTrue(all(both))
        self.assertIn('is nearby', both[0].message)
        self.assertNotIn('shared interests', both[0].message)
        self.assertTrue(self.met(self.me, self.friend))

    def test_strangers_need_both_check_ins(self):
        self.report(self.me)
        self.assertIsNone(self.alerted(self.stranger, self.me))
        self.assertFalse(self.met(self.me, self.stranger))

        client_for(self.me).post('/api/proximity/check-in/')
        self.report(self.me)
        self.assertIsNone(self.alerted(self.stranger, self.me))

        client_for(self.stranger).post('/api/proximity/check-in/')
        self.report(self.me)
        self.assertIn('shared interests', self.alerted(self.stranger, self.me).message)
        self.assertTrue(self.met(self.me, self.stranger))

    def test_a_ghost_friend_is_neither_alerted_about_nor_told(self):
        User.objects.filter(pk=self.friend.pk).update(ghost_mode=True)
        self.report(self.me)
        self.assertIsNone(self.alerted(self.me, self.friend))
        self.assertIsNone(self.alerted(self.friend, self.me))

    def test_a_friend_whose_position_is_stale_is_not_announced(self):
        # The map keeps a friend's last pin for a day; an alert is about now.
        User.objects.filter(pk=self.friend.pk).update(location_updated_at=timezone.now() - timedelta(hours=1))
        self.report(self.me)
        self.assertIsNone(self.alerted(self.me, self.friend))
