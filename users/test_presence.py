"""Presence and motion visibility, and session teardown on sign-out.

The load-bearing assertion throughout is `assertNotIn`, not "is null". A hidden
presence must be *absent* from the payload: a grey "offline" dot rendered for
someone who is hiding would both state something false and make the hiding
detectable, since they would read as permanently offline to everyone.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from friends.models import BlockedUser, FriendRequest, HiRequest
from users import visibility
from users.middleware import WRITE_EVERY
from users.models import User
from users.testing import check_in


def make_user(name, **extra):
    # On the map by default. Users are checked *out* on creation now (see
    # users.visibility), so a fixture for a test about anything else has to
    # opt in or it measures the check-in gate instead of its own subject.
    # proximity/test_check_in.py owns the assertions about the default.
    return check_in(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    ))


def make_friends(a, b):
    FriendRequest.objects.create(sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED)


def make_connected(a, b):
    HiRequest.objects.create(sender=a, receiver=b, status=HiRequest.Status.ACCEPTED)


class PresenceVisibilityTests(TestCase):
    """`visibility.presence_for` is the chokepoint every surface goes through,
    so the audience matrix is asserted against it directly."""

    def setUp(self):
        self.me = make_user('me')
        self.target = make_user('target', last_seen=timezone.now())

    def test_everyone_audience_reaches_a_stranger(self):
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.save()
        presence = visibility.presence_for(self.me, self.target)
        self.assertIsNotNone(presence)
        self.assertTrue(presence['is_online'])

    def test_friends_audience_hides_from_a_stranger(self):
        self.target.visibility_presence = User.Audience.FRIENDS
        self.target.save()
        self.assertIsNone(visibility.presence_for(self.me, self.target))

    def test_friends_audience_hides_from_a_mere_connection(self):
        # A connection is approved enough to open a private account, but it is
        # not friendship — the Friends audience must mean friends.
        self.target.visibility_presence = User.Audience.FRIENDS
        self.target.save()
        make_connected(self.me, self.target)
        self.assertIsNone(visibility.presence_for(self.me, self.target))

    def test_friends_audience_reaches_a_friend(self):
        self.target.visibility_presence = User.Audience.FRIENDS
        self.target.save()
        make_friends(self.me, self.target)
        self.assertIsNotNone(visibility.presence_for(self.me, self.target))

    def test_nobody_audience_hides_even_from_a_friend(self):
        self.target.visibility_presence = User.Audience.NOBODY
        self.target.save()
        make_friends(self.me, self.target)
        self.assertIsNone(visibility.presence_for(self.me, self.target))

    def test_nobody_audience_still_shows_yourself_your_own_presence(self):
        self.target.visibility_presence = User.Audience.NOBODY
        self.target.save()
        self.assertIsNotNone(visibility.presence_for(self.target, self.target))

    def test_private_account_hides_presence_from_non_approved_viewers(self):
        # Even on Everyone: the private gate outranks the field's own audience.
        self.target.is_private = True
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.save()
        self.assertIsNone(visibility.presence_for(self.me, self.target))

    def test_private_account_shows_presence_to_an_approved_viewer(self):
        self.target.is_private = True
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.save()
        make_connected(self.me, self.target)
        self.assertIsNotNone(visibility.presence_for(self.me, self.target))

    def test_paused_sharing_hides_presence(self):
        # Paused is the old Invisible: off the map, and not announcing that
        # they are around either.
        self.target.location_sharing = User.LocationSharing.PAUSED
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.save()
        self.assertIsNone(visibility.presence_for(self.me, self.target))

    def test_stale_last_seen_reads_offline_and_reports_when(self):
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.last_seen = timezone.now() - timedelta(hours=3)
        self.target.save()
        presence = visibility.presence_for(self.me, self.target)
        self.assertFalse(presence['is_online'])
        self.assertIsNotNone(presence['last_seen'])

    def test_online_presence_withholds_the_exact_timestamp(self):
        # Useful only once they are gone. Withheld while online so the payload
        # cannot be polled to reconstruct session boundaries minute by minute.
        self.target.visibility_presence = User.Audience.EVERYONE
        self.target.save()
        self.assertIsNone(visibility.presence_for(self.me, self.target)['last_seen'])

    def test_never_seen_user_reads_offline(self):
        never = make_user('never', visibility_presence=User.Audience.EVERYONE)
        presence = visibility.presence_for(self.me, never)
        self.assertFalse(presence['is_online'])
        self.assertIsNone(presence['last_seen'])


class BlockedPresenceTests(TestCase):
    """Blocking is mutually invisible, so it must strip presence and motion in
    both directions — not just for the person who placed the block."""

    def setUp(self):
        self.blocker = make_user('blocker', last_seen=timezone.now(),
                                 visibility_presence=User.Audience.EVERYONE)
        self.blocked = make_user('blocked', last_seen=timezone.now(),
                                 visibility_presence=User.Audience.EVERYONE)
        # Friends first: a block has to override an existing friendship, which
        # would otherwise satisfy every audience check below.
        make_friends(self.blocker, self.blocked)
        self.blocker.motion_class = User.MotionClass.FAST
        self.blocker.motion_updated_at = timezone.now()
        self.blocker.save()
        self.blocked.motion_class = User.MotionClass.FAST
        self.blocked.motion_updated_at = timezone.now()
        self.blocked.save()
        BlockedUser.objects.create(blocker=self.blocker, blocked=self.blocked)

    def test_blocker_sees_no_presence_or_motion(self):
        self.assertIsNone(visibility.presence_for(self.blocker, self.blocked))
        self.assertIsNone(visibility.motion_for(self.blocker, self.blocked))

    def test_blocked_user_sees_no_presence_or_motion(self):
        self.assertIsNone(visibility.presence_for(self.blocked, self.blocker))
        self.assertIsNone(visibility.motion_for(self.blocked, self.blocker))


class MotionVisibilityTests(TestCase):
    """Motion is strictly narrower than presence: approved tier only, never
    while approximate, never stale."""

    def setUp(self):
        self.me = make_user('me')
        self.target = make_user(
            'target',
            last_seen=timezone.now(),
            visibility_presence=User.Audience.EVERYONE,
            motion_class=User.MotionClass.FAST,
            motion_updated_at=timezone.now(),
        )

    def test_stranger_gets_no_motion_even_when_presence_is_public(self):
        # "Someone nearby is online" is ambient; "that stranger is in a vehicle
        # heading that way" is a targeting primitive.
        self.assertIsNotNone(visibility.presence_for(self.me, self.target))
        self.assertIsNone(visibility.motion_for(self.me, self.target))

    def test_friend_gets_motion(self):
        make_friends(self.me, self.target)
        self.assertEqual(visibility.motion_for(self.me, self.target), 'fast')

    def test_connection_gets_motion(self):
        make_connected(self.me, self.target)
        self.assertEqual(visibility.motion_for(self.me, self.target), 'fast')

    def test_approximate_sharing_suppresses_motion_for_friends(self):
        # A directional trail across snapped 500m cells would partly undo the
        # fuzzing the user asked for.
        make_friends(self.me, self.target)
        self.target.location_sharing = User.LocationSharing.APPROXIMATE
        self.target.save()
        self.assertIsNone(visibility.motion_for(self.me, self.target))
        # Presence itself survives — it is not a location claim.
        self.assertIsNotNone(visibility.presence_for(self.me, self.target))

    def test_stale_motion_is_dropped(self):
        make_friends(self.me, self.target)
        self.target.motion_updated_at = timezone.now() - timedelta(minutes=10)
        self.target.save()
        self.assertIsNone(visibility.motion_for(self.me, self.target))

    def test_motion_is_hidden_when_presence_is_hidden(self):
        make_friends(self.me, self.target)
        self.target.visibility_presence = User.Audience.NOBODY
        self.target.save()
        self.assertIsNone(visibility.motion_for(self.me, self.target))

    def test_paused_sharing_hides_motion(self):
        make_friends(self.me, self.target)
        self.target.location_sharing = User.LocationSharing.PAUSED
        self.target.save()
        self.assertIsNone(visibility.motion_for(self.me, self.target))


class NearbyPayloadPresenceTests(TestCase):
    """The wire format, not just the helper: hidden means the key is gone."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me', latitude='10.0', longitude='10.0')
        self.other = make_user(
            'other',
            latitude='10.001',
            longitude='10.001',
            last_seen=timezone.now(),
            motion_class=User.MotionClass.FAST,
            motion_updated_at=timezone.now(),
        )
        self.client.force_authenticate(self.me)

    def _entry(self):
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        return {e['id']: e for e in response.json()}[self.other.pk]

    def test_presence_key_absent_for_stranger_on_friends_default(self):
        entry = self._entry()
        self.assertEqual(entry['relationship'], 'stranger')
        self.assertNotIn('presence', entry)
        self.assertNotIn('motion_class', entry)

    def test_presence_present_for_stranger_on_everyone(self):
        self.other.visibility_presence = User.Audience.EVERYONE
        self.other.save()
        entry = self._entry()
        self.assertTrue(entry['presence']['is_online'])
        # Still no motion: stranger tier.
        self.assertNotIn('motion_class', entry)

    def test_friend_gets_both(self):
        make_friends(self.me, self.other)
        entry = self._entry()
        self.assertTrue(entry['presence']['is_online'])
        self.assertEqual(entry['motion_class'], 'fast')

    def test_private_account_payload_omits_presence(self):
        self.other.is_private = True
        self.other.visibility_presence = User.Audience.EVERYONE
        self.other.save()
        entry = self._entry()
        self.assertNotIn('presence', entry)


class PresenceHeartbeatTests(TestCase):
    """The heartbeat rides existing traffic, but must not count traffic from an
    app that is not open."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def _last_seen(self):
        return User.objects.get(pk=self.me.pk).last_seen

    def test_foreground_header_marks_presence(self):
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        self.assertIsNotNone(self._last_seen())

    def test_request_without_the_header_does_not_mark_presence(self):
        # This is the background location task, which keeps calling the API
        # every 30s with the app closed. Counting it would pin those users
        # online forever.
        self.client.get('/api/users/me/')
        self.assertIsNone(self._last_seen())

    def test_background_header_backdates_out_of_the_online_window(self):
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        self.assertTrue(visibility.is_online(User.objects.get(pk=self.me.pk)))

        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='background')
        refreshed = User.objects.get(pk=self.me.pk)
        self.assertFalse(visibility.is_online(refreshed))
        # Backdated, not nulled: "last seen a moment ago" is true and still
        # renders, whereas None would claim they had never opened the app.
        self.assertIsNotNone(refreshed.last_seen)

    def test_backgrounding_clears_motion(self):
        self.me.motion_class = User.MotionClass.FAST
        self.me.motion_updated_at = timezone.now()
        self.me.save()
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='background')
        refreshed = User.objects.get(pk=self.me.pk)
        self.assertEqual(refreshed.motion_class, User.MotionClass.STATIONARY)
        self.assertIsNone(refreshed.motion_updated_at)

    def test_writes_are_throttled(self):
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        first = self._last_seen()
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        self.assertEqual(self._last_seen(), first)

    def test_write_resumes_once_the_throttle_window_passes(self):
        stale = timezone.now() - WRITE_EVERY - timedelta(seconds=1)
        User.objects.filter(pk=self.me.pk).update(last_seen=stale)
        self.client.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        self.assertGreater(self._last_seen(), stale)

    def test_anonymous_request_is_ignored(self):
        anon = APIClient()
        response = anon.get('/api/users/me/', HTTP_X_AURA_PRESENCE='foreground')
        self.assertEqual(response.status_code, 401)


class SpeedClassificationTests(TestCase):
    """Raw speed is bucketed on arrival and never stored."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def _patch(self, **extra):
        response = self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0', **extra},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        return User.objects.get(pk=self.me.pk)

    def test_vehicle_speed_classifies_as_fast(self):
        self.assertEqual(self._patch(speed=15.0).motion_class, 'fast')

    def test_walking_speed_classifies_as_moving(self):
        self.assertEqual(self._patch(speed=3.0).motion_class, 'moving')

    def test_near_zero_speed_classifies_as_stationary(self):
        self.assertEqual(self._patch(speed=0.4).motion_class, 'stationary')

    def test_absent_speed_leaves_motion_untouched(self):
        # No estimate is not the same claim as "measured as zero", so a fix
        # without one must not silently reset a class set moments ago.
        self.me.motion_class = User.MotionClass.FAST
        self.me.motion_updated_at = timezone.now()
        self.me.save()
        self.assertEqual(self._patch().motion_class, 'fast')

    def test_negative_sentinel_speed_leaves_motion_untouched(self):
        # expo-location reports -1 when it has no speed estimate.
        self.me.motion_class = User.MotionClass.FAST
        self.me.motion_updated_at = timezone.now()
        self.me.save()
        self.assertEqual(self._patch(speed=-1).motion_class, 'fast')

    def test_raw_speed_is_not_echoed_back(self):
        response = self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0', 'speed': 15.0},
            format='json',
        )
        self.assertNotIn('speed', response.json())


class LogoutTests(TestCase):
    """Sign-out has to end the session server-side. Deleting the token on the
    device only stops that device from using it."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me', push_token='ExponentPushToken[abc123]')
        self.refresh = RefreshToken.for_user(self.me)
        self.client.force_authenticate(self.me)

    def test_logout_blacklists_the_refresh_token(self):
        response = self.client.post(
            '/api/users/logout/', {'refresh': str(self.refresh)}, format='json'
        )
        self.assertEqual(response.status_code, 204)

    def test_revoked_refresh_token_is_rejected(self):
        self.client.post(
            '/api/users/logout/', {'refresh': str(self.refresh)}, format='json'
        )
        # A fresh, unauthenticated client — the refresh endpoint is the one a
        # stolen token would be replayed against.
        anon = APIClient()
        response = anon.post(
            '/api/users/token/refresh/', {'refresh': str(self.refresh)}, format='json'
        )
        self.assertEqual(response.status_code, 401)

    def test_logout_unregisters_the_push_token(self):
        self.client.post(
            '/api/users/logout/', {'refresh': str(self.refresh)}, format='json'
        )
        self.assertIsNone(User.objects.get(pk=self.me.pk).push_token)

    def test_logout_marks_the_user_offline(self):
        User.objects.filter(pk=self.me.pk).update(last_seen=timezone.now())
        self.client.post(
            '/api/users/logout/', {'refresh': str(self.refresh)}, format='json'
        )
        self.assertFalse(visibility.is_online(User.objects.get(pk=self.me.pk)))

    def test_push_token_is_cleared_even_when_the_refresh_token_is_bad(self):
        # The offline case: the app retries with whatever it had. The session
        # cannot be revoked, but the device must still stop receiving pushes.
        response = self.client.post(
            '/api/users/logout/', {'refresh': 'not-a-token'}, format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(User.objects.get(pk=self.me.pk).push_token)

    def test_push_token_is_cleared_when_no_refresh_token_is_sent(self):
        response = self.client.post('/api/users/logout/', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(User.objects.get(pk=self.me.pk).push_token)
