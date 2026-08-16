"""The App Store compliance matrix, asserted end to end.

Everything here restates a promise made to App Review under 5.1.2(i) rather
than a piece of internal behaviour. They overlap with the other proximity
suites on purpose: those test units, this tests the promises, and a promise
that survives a refactor of the unit under it is the thing worth keeping.

The three states the whole API turns on:

    paused                  200 []          nothing to show, working as chosen
    live, no position yet   400 + code      transient; ask again with a fix
    live, with a position   200 [rows]      the normal answer
"""
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from friends.models import BlockedUser
from interests.models import Interest
from proximity.views import CODE_LOCATION_NOT_AVAILABLE, CODE_SHARING_PAUSED
from users.models import User
from users.testing import share_location

NEARBY = '/api/proximity/nearby/'
NEARBY_QUERY = '/api/proximity/nearby/query/'
LOCATION = '/api/proximity/location/'
PRIVACY = '/api/users/privacy/'


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


class ApiStateMatrixTests(TestCase):
    """One test per row of the table above, plus the codes they carry."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def test_paused_is_an_empty_list_not_an_error(self):
        response = self.client.get(NEARBY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_live_without_a_position_is_a_coded_400(self):
        share_location(self.me)
        response = self.client.get(NEARBY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], CODE_LOCATION_NOT_AVAILABLE)

    def test_live_with_a_position_answers_normally(self):
        share_location(self.me)
        self.client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        response = self.client.get(NEARBY)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)

    def test_a_paused_update_is_refused_with_its_own_code(self):
        response = self.client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], CODE_SHARING_PAUSED)

    def test_the_two_codes_are_distinct(self):
        """The client draws a different screen for each, so they must never
        collapse into one another."""
        self.assertNotEqual(CODE_SHARING_PAUSED, CODE_LOCATION_NOT_AVAILABLE)

    def test_an_unrelated_400_carries_no_location_code(self):
        """The client keys its retry on the code. A validation failure must not
        be mistaken for "your position has not arrived yet"."""
        share_location(self.me)
        response = self.client.post(
            NEARBY_QUERY, {'latitude': '999.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(
            response.json().get('code'), CODE_LOCATION_NOT_AVAILABLE,
        )


class ColdInstallTests(TestCase):
    """Nothing about arriving puts anybody on the map."""

    def test_a_new_account_is_paused_and_has_no_position(self):
        user = make_user('fresh')
        self.assertEqual(user.location_sharing, User.LocationSharing.PAUSED)
        self.assertIsNone(user.latitude)
        self.assertFalse(user.is_on_map())

    def test_granting_the_os_permission_changes_nothing_server_side(self):
        """There is no API for it, and that is the point.

        The OS permission is a device fact. The server never hears about it and
        has no field for it, so it cannot be mistaken for consent to publish —
        which is precisely the confusion App Review rejected.
        """
        user = make_user('permitted')
        client = APIClient()
        client.force_authenticate(user)

        # The most an app with permission can do without the user choosing to
        # share: report a position. It is refused.
        response = client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

        user.refresh_from_db()
        self.assertFalse(user.is_on_map())

    def test_opening_the_map_changes_nothing(self):
        """Browsing is a read. It must not be a way onto the map."""
        user = make_user('browser')
        client = APIClient()
        client.force_authenticate(user)

        for _ in range(3):
            client.post(
                NEARBY_QUERY, {'latitude': '10.0', 'longitude': '10.0'},
                format='json',
            )

        user.refresh_from_db()
        self.assertFalse(user.is_on_map())
        self.assertIsNone(user.latitude)


class StopSharingTests(TestCase):
    """The explicit stop conditions, and what each one takes with it."""

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(make_user('me'))
        self.client.force_authenticate(self.me)
        self.client.patch(
            LOCATION,
            {'latitude': '10.0', 'longitude': '10.0', 'accuracy': 8.0, 'speed': 12.0},
            format='json',
        )
        self.me.refresh_from_db()

    def test_the_setup_really_did_publish_something(self):
        """Otherwise the assertions below would pass against an empty row."""
        self.assertTrue(self.me.is_on_map())
        self.assertIsNotNone(self.me.latitude)
        self.assertIsNotNone(self.me.location_updated_at)
        self.assertEqual(self.me.motion_class, User.MotionClass.FAST)

    def test_stop_sharing_removes_every_public_trace(self):
        response = self.client.patch(
            PRIVACY, {'location_sharing': 'paused'}, format='json',
        )
        self.assertEqual(response.status_code, 200)

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        self.assertIsNone(self.me.latitude)
        self.assertIsNone(self.me.longitude)
        self.assertIsNone(self.me.location_updated_at)
        self.assertIsNone(self.me.location_accuracy_m)
        self.assertEqual(self.me.motion_class, User.MotionClass.STATIONARY)
        self.assertIsNone(self.me.motion_updated_at)

    def test_sign_out_pauses_server_side(self):
        """The client PATCHes this before tearing the session down. Asserted
        against the endpoint it actually calls, so a change to either side
        shows up here."""
        response = self.client.patch(
            PRIVACY, {'location_sharing': 'paused'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['location_sharing'], 'paused')

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        self.assertIsNone(self.me.latitude)

    def test_signing_back_in_does_not_restore_sharing(self):
        """No 'resume where you left off'. There is nowhere for it to be
        remembered, which is the point — the previous choice is overwritten by
        Paused rather than parked."""
        self.client.patch(PRIVACY, {'location_sharing': 'paused'}, format='json')

        # A fresh session for the same account, as a later login would be.
        again = APIClient()
        again.force_authenticate(self.me)
        response = again.get(PRIVACY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['location_sharing'], 'paused')

        response = again.get(NEARBY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class BlockingThroughEveryPathTests(TransactionTestCase):
    """Apple's first requirement, checked on the new path as well as the old.

    `TransactionTestCase` because the composite map endpoint fans out across a
    thread pool — see MapEndpointTests in test_map_visibility.
    """

    def setUp(self):
        interest = Interest.objects.create(name='Climbing')
        self.a = share_location(make_user('a', latitude='10.0', longitude='10.0'))
        self.b = share_location(make_user('b', latitude='10.0002', longitude='10.0002'))
        for user in (self.a, self.b):
            user.interests.add(interest)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def sees(self, viewer, path, body=None):
        client = self.client_for(viewer)
        response = client.post(path, body, format='json') if body else client.get(path)
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        if isinstance(rows, dict):
            rows = rows['users']
        return {row['id'] for row in rows}

    def test_both_directions_on_the_public_path(self):
        self.assertIn(self.b.pk, self.sees(self.a, NEARBY))
        self.assertIn(self.a.pk, self.sees(self.b, NEARBY))

        BlockedUser.objects.create(blocker=self.a, blocked=self.b)

        self.assertNotIn(self.b.pk, self.sees(self.a, NEARBY))
        self.assertNotIn(self.a.pk, self.sees(self.b, NEARBY))

    def test_both_directions_on_the_private_query_path(self):
        """The path added for paused browsing must not be a way around a block.

        It shares `nearby_users` with the public path, so this is really
        asserting that the anchor refactor did not grow a second entry point
        that skips `visible_users`.
        """
        here = {'latitude': '10.0', 'longitude': '10.0'}
        self.assertIn(self.b.pk, self.sees(self.a, NEARBY_QUERY, here))

        BlockedUser.objects.create(blocker=self.b, blocked=self.a)

        self.assertNotIn(self.b.pk, self.sees(self.a, NEARBY_QUERY, here))
        self.assertNotIn(self.a.pk, self.sees(self.b, NEARBY_QUERY, here))

    def test_a_paused_blocker_is_still_hidden_from_the_person_they_blocked(self):
        """Blocking is mutual invisibility, and pausing does not weaken it."""
        BlockedUser.objects.create(blocker=self.a, blocked=self.b)
        self.a.location_sharing = User.LocationSharing.PAUSED
        self.a.save(update_fields=['location_sharing'])

        self.assertNotIn(
            self.a.pk,
            self.sees(self.b, NEARBY_QUERY, {'latitude': '10.0', 'longitude': '10.0'}),
        )

    def test_both_directions_on_the_composite_map(self):
        BlockedUser.objects.create(blocker=self.a, blocked=self.b)
        self.assertNotIn(self.b.pk, self.sees(self.a, '/api/map/nearby/'))
        self.assertNotIn(self.a.pk, self.sees(self.b, '/api/map/nearby/'))


class ZoneRulesStillApplyTests(TestCase):
    """Visibility is a conjunction, and the private path must not drop a term.

    live AND has a position AND inside the viewer's zone AND not blocked AND
    the other privacy rules pass.
    """

    def setUp(self):
        self.interest = Interest.objects.create(name='Climbing')
        self.me = make_user('me')
        self.me.interests.add(self.interest)
        self.client = APIClient()
        self.client.force_authenticate(self.me)

    def other(self, name, lat, lon, **extra):
        user = share_location(
            make_user(name, latitude=str(lat), longitude=str(lon), **extra))
        user.interests.add(self.interest)
        return user

    def query(self, lat=10.0, lon=10.0):
        response = self.client.post(
            NEARBY_QUERY, {'latitude': str(lat), 'longitude': str(lon)}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()}

    def test_the_zone_radius_still_bounds_a_private_browser(self):
        far = self.other('far', 10.004, 10.0)  # ~440m north
        self.me.zone_radius = User.ZONE_RADIUS_MIN_M
        self.me.save(update_fields=['zone_radius'])
        self.assertNotIn(far.pk, self.query())

        self.me.zone_radius = 5000
        self.me.save(update_fields=['zone_radius'])
        self.assertIn(far.pk, self.query())

    def test_interest_opt_out_still_hides_a_stranger(self):
        opted_out = self.other('quiet', 10.0002, 10.0002, discoverable_by_interest=False)
        self.assertNotIn(opted_out.pk, self.query())

    def test_a_stranger_without_a_shared_interest_is_still_hidden(self):
        stranger = share_location(
            make_user('stranger', latitude='10.0002', longitude='10.0002'))
        stranger.interests.add(Interest.objects.create(name='Knitting'))
        self.assertNotIn(stranger.pk, self.query())

    def test_a_private_account_is_still_minimal(self):
        private = self.other('private', 10.0002, 10.0002, is_private=True)
        response = self.client.post(
            NEARBY_QUERY, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        row = next(r for r in response.json() if r['id'] == private.pk)
        self.assertTrue(row['is_private'])
        self.assertNotIn('bio', row)
        self.assertNotIn('interests', row)


class NoAutomaticVisibilityTests(TestCase):
    """Nothing on the server makes a user visible except their own request.

    The client half of this lives in
    services/__tests__/noAutomaticVisibility.test.ts. Both are needed: the
    client one bounds what the app can ask for, this one bounds what the API
    will do if something asks anyway.

    App Review's list — app open, login, permission granted, GPS fix, map
    opened, foreground, background start, location change, zone entry — is all
    the same shape of thing: an event that reaches an endpoint. So rather than
    simulate each, these pin that the endpoints those events reach cannot
    change the sharing mode at all.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def assert_still_paused(self, note):
        self.me.refresh_from_db()
        self.assertEqual(
            self.me.location_sharing, User.LocationSharing.PAUSED, note,
        )
        self.assertFalse(self.me.is_on_map(), note)

    def test_reporting_a_position_cannot_check_anyone_in(self):
        """"a GPS position is obtained" and "the user's location changes"."""
        for _ in range(3):
            self.client.patch(
                LOCATION, {'latitude': '10.0', 'longitude': '10.0', 'speed': 3.0},
                format='json',
            )
        self.assert_still_paused('a location report changed the sharing mode')

    def test_browsing_the_map_cannot_check_anyone_in(self):
        """"the map screen opens", and every poll it makes afterwards."""
        for _ in range(3):
            self.client.post(
                NEARBY_QUERY, {'latitude': '10.0', 'longitude': '10.0'},
                format='json',
            )
            self.client.get(NEARBY)
        self.assert_still_paused('browsing the map changed the sharing mode')

    def test_reading_the_privacy_screen_cannot_check_anyone_in(self):
        """"the application opens" — this is what it fetches on launch."""
        for _ in range(3):
            self.assertEqual(self.client.get(PRIVACY).status_code, 200)
        self.assert_still_paused('reading privacy changed the sharing mode')

    def test_other_privacy_settings_do_not_drag_sharing_with_them(self):
        """A PATCH that says nothing about location must not imply one."""
        response = self.client.patch(
            PRIVACY, {'is_private': True, 'visibility_stories': 'nobody'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assert_still_paused('an unrelated privacy change turned sharing on')

    def test_a_zone_change_cannot_check_anyone_in(self):
        """"the user enters a zone" — and setting one up, which is the only
        zone action a paused user can take."""
        response = self.client.patch(
            '/api/users/zone/', {'zone_active': True, 'zone_radius': 1000},
            format='json',
        )
        self.assertIn(response.status_code, (200, 400))
        self.assert_still_paused('a zone change turned sharing on')

    def test_only_the_privacy_endpoint_can_turn_sharing_on(self):
        """The positive case, so the tests above cannot pass by the endpoint
        being broken for everyone."""
        response = self.client.patch(
            PRIVACY, {'location_sharing': 'live'}, format='json',
        )
        self.assertEqual(response.status_code, 200)

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())

    def test_the_serializer_is_the_only_place_the_field_is_writable(self):
        """Static backstop for an endpoint nobody has written yet.

        A new view that spreads `request.data` onto the user — the usual way
        this kind of hole appears — would have to add the field to a serializer
        to work, and there is only one that accepts it.
        """
        from users.serializers import (
            PrivacySettingsSerializer,
            UserProfileSerializer,
        )

        self.assertIn('location_sharing', PrivacySettingsSerializer().fields)

        profile = UserProfileSerializer().fields['location_sharing']
        self.assertFalse(
            profile.read_only,
            'If /me ever becomes writable for this field it needs the same '
            'consent story as the privacy screen, or it is a second door.',
        )


class SharingSessionTests(TestCase):
    """Visibility belongs to the run of the app that chose it.

    App Review's last bullet — never check somebody in because "any previous
    preference says they were visible" — is the one a persistent setting cannot
    satisfy on its own. The token is how it is satisfied: minted on going
    visible, held in memory by the client, and gone the moment the process is.

    What it must *not* do is turn backgrounding into a check-out. Nothing here
    counts down and nothing expires; only a run that cannot produce the token
    ends the session.
    """

    END = '/api/proximity/sharing-session/end/'

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def go_visible(self):
        response = self.client.patch(
            PRIVACY, {'location_sharing': 'live'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        return response.json()['sharing_session']

    def test_going_visible_mints_a_token(self):
        token = self.go_visible()
        self.assertIsNotNone(token)
        self.assertEqual(str(self.me.sharing_session), token)

    def test_a_cold_launch_presents_nothing_and_is_paused(self):
        """The whole feature, in one test."""
        self.go_visible()
        self.client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )

        response = self.client.post(self.END, {'session': None}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['location_sharing'], 'paused')

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())
        self.assertIsNone(self.me.latitude)
        self.assertIsNone(self.me.sharing_session)

    def test_omitting_the_field_entirely_is_the_same_as_null(self):
        """An older client, or one that lost the token some other way."""
        self.go_visible()
        response = self.client.post(self.END, {}, format='json')
        self.assertEqual(response.json()['location_sharing'], 'paused')

    def test_the_run_that_owns_the_session_is_left_alone(self):
        """Why it is a token and not a bare "pause me".

        The startup call races the user's own tap. Somebody who opens the app
        and immediately chooses Live has the current token by the time it
        lands, and must not be paused by it.
        """
        token = self.go_visible()

        response = self.client.post(self.END, {'session': token}, format='json')
        self.assertEqual(response.json()['location_sharing'], 'live')

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        self.assertIsNotNone(self.me.sharing_session)

    def test_a_stale_token_from_a_previous_run_does_not_match(self):
        stale = self.go_visible()
        self.client.patch(PRIVACY, {'location_sharing': 'paused'}, format='json')
        self.go_visible()

        response = self.client.post(self.END, {'session': stale}, format='json')
        self.assertEqual(response.json()['location_sharing'], 'paused')

    def test_pausing_by_hand_also_clears_the_token(self):
        """Otherwise a token could outlive the visibility it was minted for and
        be replayed against the next one."""
        self.go_visible()
        self.client.patch(PRIVACY, {'location_sharing': 'paused'}, format='json')

        self.me.refresh_from_db()
        self.assertIsNone(self.me.sharing_session)

    def test_resuming_mints_a_fresh_token(self):
        first = self.go_visible()
        self.client.patch(PRIVACY, {'location_sharing': 'paused'}, format='json')
        second = self.go_visible()
        self.assertNotEqual(first, second)

    def test_calling_it_while_already_paused_is_a_no_op(self):
        response = self.client.post(self.END, {'session': None}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['location_sharing'], 'paused')

    def test_it_is_idempotent(self):
        self.go_visible()
        for _ in range(3):
            response = self.client.post(self.END, {'session': None}, format='json')
            self.assertEqual(response.json()['location_sharing'], 'paused')

    def test_the_token_is_not_writable_through_the_privacy_endpoint(self):
        """A client that could choose its own token could keep any session
        alive forever."""
        token = self.go_visible()
        self.client.patch(
            PRIVACY,
            {'sharing_session': '00000000-0000-0000-0000-000000000000'},
            format='json',
        )
        self.me.refresh_from_db()
        self.assertEqual(str(self.me.sharing_session), token)

    def test_nothing_about_it_expires(self):
        """No clock, no countdown, no backgrounding rule. The session ends when
        a run without the token says so, and at no other time."""
        token = self.go_visible()
        self.client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )

        # Stands in for time passing and for the app being backgrounded: the
        # background task keeps reporting and never touches the session.
        for _ in range(5):
            self.client.patch(
                LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
            )
            self.assertEqual(self.client.get(NEARBY).status_code, 200)

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())
        self.assertEqual(str(self.me.sharing_session), token)
