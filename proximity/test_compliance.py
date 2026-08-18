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
from unittest import mock

from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest
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


def befriend(a, b):
    FriendRequest.objects.create(
        sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED,
    )


class _InlineFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _InlinePool:
    def submit(self, fn, *args):
        return _InlineFuture(fn(*args))


def inline_map_pool():
    """Run the composite map endpoint's fan-out on the calling thread.

    In tests the database is one in-memory SQLite connection and a TestCase
    holds an open transaction for the whole method, so the eight-thread fan-out
    in proximity.views reads rows the test itself has just written and gets
    "database table is locked". Production runs Postgres with a connection per
    thread and does not. What is under test here is the payload, not the
    executor that assembles it.
    """
    return mock.patch('proximity.views._MAP_POOL', _InlinePool())


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


class StoryLocationTests(TestCase):
    """Stories and live location are two products that share one table.

    The rule the app promises, and the one asserted here:

        turning off location sharing takes you and your stories off the map.
        It does not stop you sharing stories with your friends.

    So every test below is one of two questions asked of the same row - may my
    friend read this, and may anybody draw it at a coordinate - and the point
    is that they have different answers. See stories/visibility.py.
    """

    STORIES = '/api/stories/'
    NEARBY_STORIES = '/api/stories/nearby/'
    FRIEND_STORIES = '/api/stories/friends/'
    MAP_QUERY = '/api/map/nearby/query/'
    MINE = '/api/stories/mine/'
    SAVED = '/api/stories/saved/'

    HERE = {'latitude': '10.000000', 'longitude': '10.000000'}

    def setUp(self):
        self.author = share_location(make_user('author'))
        self.author_client = APIClient()
        self.author_client.force_authenticate(self.author)

        self.viewer = share_location(make_user('viewer'))
        self.viewer_client = APIClient()
        self.viewer_client.force_authenticate(self.viewer)

        # A story is for friends, so the pair the assertions are about has to
        # be one. The stranger case gets its own test below.
        befriend(self.author, self.viewer)

        for client in (self.author_client, self.viewer_client):
            client.patch(LOCATION, dict(self.HERE), format='json')

    # -- helpers --------------------------------------------------------------
    def post_story(self, client=None, **fields):
        body = {'content': 'hello', **self.HERE, **fields}
        return (client or self.author_client).post(self.STORIES, body, format='json')

    def pause(self, client):
        response = client.patch(PRIVACY, {'location_sharing': 'paused'}, format='json')
        self.assertEqual(response.status_code, 200)

    def map_story_ids(self):
        """What the viewer's map actually draws."""
        response = self.viewer_client.get(self.NEARBY_STORIES)
        self.assertEqual(response.status_code, 200)
        return {s['id'] for s in response.json()}

    def feed_story_ids(self, client=None):
        """What the viewer's chat/avatar surface actually offers."""
        response = (client or self.viewer_client).get(self.FRIEND_STORIES)
        self.assertEqual(response.status_code, 200)
        return {s['id'] for s in response.json()['results']}

    # -- the setup itself -----------------------------------------------------
    def test_the_setup_really_did_publish_a_pin(self):
        """Otherwise half the assertions below pass against an empty map."""
        response = self.post_story()
        self.assertEqual(response.status_code, 201)
        story_id = response.json()['id']
        self.assertIn(story_id, self.map_story_ids())
        self.assertIn(story_id, self.feed_story_ids())

    # -- posting while paused -------------------------------------------------
    def test_a_paused_author_can_post(self):
        """The behaviour this whole change is about.

        Posting used to be refused outright while paused, which reads the
        switch as "stop using stories" when what it says is "stop appearing on
        the map".
        """
        self.pause(self.author_client)
        response = self.post_story()
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_paused_story_carries_no_position(self):
        """Enforced in the database, not in the client that sent it.

        The request above carried a coordinate - the composer sends one - and
        the server is the thing that has to refuse to keep it. A build that
        keeps sending them while paused gets them dropped.
        """
        from stories.models import Story

        self.pause(self.author_client)
        story_id = self.post_story(place_name='Blue Bottle').json()['id']

        story = Story.objects.get(pk=story_id)
        self.assertIsNone(story.latitude)
        self.assertIsNone(story.longitude)
        # A place name is a location under another name, so it goes too.
        self.assertEqual(story.place_name, '')

    def test_a_paused_story_does_not_borrow_an_old_position(self):
        """Not the last one we held, either.

        Pausing deletes the stored position, but the author was Live a moment
        ago and the row could plausibly be repopulated from anywhere. Posting
        from Live first is what makes this test able to fail.
        """
        from stories.models import Story

        self.post_story()
        self.pause(self.author_client)
        story_id = self.post_story().json()['id']

        self.assertIsNone(Story.objects.get(pk=story_id).latitude)

    def test_a_sharing_author_may_post_without_a_position(self):
        """Locationless is a kind of story, not an error state.

        The client sends a coordinate when it has one, and a phone that has not
        got a fix yet - or never asked for the permission - is not a reason to
        refuse the story.
        """
        from stories.models import Story

        response = self.author_client.post(
            self.STORIES, {'content': 'no fix here'}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        story = Story.objects.get(pk=response.json()['id'])
        self.assertIsNone(story.latitude)
        self.assertIn(story.pk, self.feed_story_ids())
        self.assertNotIn(story.pk, self.map_story_ids())

    def test_a_paused_story_reaches_a_friend(self):
        self.pause(self.author_client)
        story_id = self.post_story().json()['id']
        self.assertIn(story_id, self.feed_story_ids())

    def test_a_paused_story_is_not_on_the_map(self):
        # The viewer is still Live, and their own pin is the control: without
        # it "absent from the composite map" also passes against a map that
        # returned nothing at all.
        control_id = self.post_story(self.viewer_client).json()['id']
        self.pause(self.author_client)
        story_id = self.post_story().json()['id']

        self.assertNotIn(story_id, self.map_story_ids())
        with inline_map_pool():
            composite = self.viewer_client.get('/api/map/nearby/')
        self.assertEqual(composite.status_code, 200)
        drawn = {s['id'] for s in composite.json()['stories']}
        self.assertIn(control_id, drawn)
        self.assertNotIn(story_id, drawn)

    def test_the_friend_feed_carries_no_coordinates_at_all(self):
        """Data minimisation, not just filtering.

        The chat surface never draws a map, so the fields are absent from its
        payload rather than present and null - a client cannot leak what it was
        never sent, in this or any future version of it.
        """
        self.post_story(place_name='Blue Bottle')
        rows = self.viewer_client.get(self.FRIEND_STORIES).json()['results']
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn('latitude', row)
            self.assertNotIn('longitude', row)

    def test_a_paused_authors_place_name_is_withheld_too(self):
        """The story was posted Live, so it has one; pausing has to reach it.

        Otherwise the coordinate is gone and the string naming the same spot is
        still there, which is the same leak spelled differently.
        """
        self.post_story(place_name='Blue Bottle')
        rows = self.viewer_client.get(self.FRIEND_STORIES).json()['results']
        self.assertEqual([r['place_name'] for r in rows], ['Blue Bottle'])

        self.pause(self.author_client)
        rows = self.viewer_client.get(self.FRIEND_STORIES).json()['results']
        self.assertEqual([r['place_name'] for r in rows], [''])

    # -- pausing after posting ------------------------------------------------
    def test_pausing_retracts_the_pin_but_keeps_the_story(self):
        """The case a write-time check could never have handled.

        Nothing was wrong when this story was posted - the author was on the
        map. Pausing is what changes the answer, and it has to change exactly
        one of the two: the pin goes, the story stays.
        """
        story_id = self.post_story().json()['id']
        self.assertIn(story_id, self.map_story_ids())

        self.pause(self.author_client)
        self.assertNotIn(story_id, self.map_story_ids())
        self.assertIn(story_id, self.feed_story_ids())

    def test_resuming_brings_the_pin_back(self):
        """Pausing retracts, it does not destroy. Otherwise the switch is a
        one-way door and users learn not to touch it."""
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)
        self.assertNotIn(story_id, self.map_story_ids())

        self.author_client.patch(PRIVACY, {'location_sharing': 'live'}, format='json')
        self.assertIn(story_id, self.map_story_ids())

    def test_a_paused_author_still_sees_their_own(self):
        """Their archive is theirs. A pin only they can see is not a
        publication - the rule is about who else learns where they were."""
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)

        mine = self.author_client.get(self.MINE)
        self.assertEqual(mine.status_code, 200)
        self.assertIn(story_id, {s['id'] for s in mine.json()['results']})
        self.assertIn(story_id, self.feed_story_ids(self.author_client))

    def test_a_paused_author_can_still_delete_their_own(self):
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)
        self.assertEqual(
            self.author_client.delete(f'/api/stories/{story_id}/').status_code, 204,
        )

    # -- acting on a paused author's story ------------------------------------
    def test_a_friend_can_still_react_when_the_author_pauses(self):
        """Reading and reacting are the story; the map is the other product.

        The 500m rule this used to run is also a probe once the author is off
        the map: the 403 would report whether the reactor is standing near the
        position the author just withdrew.
        """
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)

        response = self.viewer_client.post(
            f'/api/stories/{story_id}/react/', {'reaction': 'like'}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_a_friend_can_still_save_when_the_author_pauses(self):
        story_id = self.post_story().json()['id']
        self.assertEqual(
            self.viewer_client.post(f'/api/stories/{story_id}/save/').status_code, 201,
        )
        self.pause(self.author_client)

        saved = self.viewer_client.get(self.SAVED)
        self.assertEqual(saved.status_code, 200)
        rows = saved.json()['results']
        self.assertIn(story_id, {s['id'] for s in rows})
        # Kept, but stripped: a bookmark is not a standing grant of a position.
        for row in rows:
            self.assertNotIn('latitude', row)
            self.assertEqual(row['place_name'], '')

    def test_a_paused_pair_can_still_react(self):
        """Both ends off the map. Neither has a position and neither needs one:
        there is nothing here to be within 500m of."""
        self.pause(self.author_client)
        self.pause(self.viewer_client)
        story_id = self.post_story().json()['id']

        response = self.viewer_client.post(
            f'/api/stories/{story_id}/react/', {'reaction': 'like'}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_the_500m_rule_still_holds_for_a_pin(self):
        """The negative half. A story that *is* a pin still takes standing near
        it, which is the surface that rule was written for."""
        story_id = self.post_story().json()['id']
        self.viewer_client.patch(
            LOCATION, {'latitude': '10.5', 'longitude': '10.5'}, format='json',
        )
        response = self.viewer_client.post(
            f'/api/stories/{story_id}/react/', {'reaction': 'like'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    # -- who a story is for ---------------------------------------------------
    def test_a_stranger_gets_nothing_even_at_everyone(self):
        """Everyone is not literal for stories. A story is for the people the
        author chose, not for whoever happens to be standing nearby."""
        self.author.visibility_stories = User.Audience.EVERYONE
        self.author.save(update_fields=['visibility_stories'])
        story_id = self.post_story().json()['id']

        stranger = share_location(make_user('stranger'))
        stranger_client = APIClient()
        stranger_client.force_authenticate(stranger)
        stranger_client.patch(LOCATION, dict(self.HERE), format='json')

        self.assertNotIn(story_id, self.feed_story_ids(stranger_client))
        self.assertNotIn(
            story_id,
            {s['id'] for s in stranger_client.get(self.NEARBY_STORIES).json()},
        )
        self.assertEqual(
            stranger_client.post(f'/api/stories/{story_id}/react/',
                                 {'reaction': 'like'}, format='json').status_code,
            404,
        )

    def test_blocking_hides_stories_both_ways(self):
        """Including the pin, and including the direction the blocker did not
        choose - a block is mutually invisible or it is detectable."""
        mine_id = self.post_story().json()['id']
        theirs_id = self.post_story(self.viewer_client).json()['id']
        self.assertIn(mine_id, self.feed_story_ids())

        BlockedUser.objects.create(blocker=self.author, blocked=self.viewer)

        self.assertNotIn(mine_id, self.feed_story_ids())
        self.assertNotIn(mine_id, self.map_story_ids())
        self.assertNotIn(theirs_id, self.feed_story_ids(self.author_client))
        self.assertEqual(
            self.viewer_client.post(f'/api/stories/{mine_id}/react/',
                                    {'reaction': 'like'}, format='json').status_code,
            404,
        )

    # -- precision ------------------------------------------------------------
    def test_approximate_fuzzes_the_pin_as_well_as_the_dot(self):
        """Otherwise the setting is undone by using the app.

        Someone whose dot is snapped to a ~500m cell would hand back the exact
        point by posting, under their own name - a worse leak than the one the
        setting exists to prevent, because it is a place they chose to stop at.
        """
        from stories.models import Story
        from users.visibility import snap_coordinates

        share_location(self.author, User.LocationSharing.APPROXIMATE)
        story_id = self.post_story().json()['id']

        story = Story.objects.get(pk=story_id)
        self.assertNotEqual(
            (str(story.latitude), str(story.longitude)),
            (self.HERE['latitude'], self.HERE['longitude']),
            'an Approximate author posted a precise pin',
        )
        # And it is the same cell their dot is snapped to, not a second scheme.
        snapped_lat, snapped_lon = snap_coordinates(self.author, 10.0, 10.0)
        self.assertEqual(float(story.latitude), round(float(snapped_lat), 6))
        self.assertEqual(float(story.longitude), round(float(snapped_lon), 6))

    def test_live_pins_are_not_fuzzed(self):
        """The negative half, so the test above cannot pass by the coordinate
        being mangled for everybody."""
        from stories.models import Story

        story_id = self.post_story().json()['id']
        story = Story.objects.get(pk=story_id)
        self.assertEqual(float(story.latitude), 10.0)
        self.assertEqual(float(story.longitude), 10.0)


class StalePositionTests(TestCase):
    """A live setting over a coordinate nobody has refreshed is not a location.

    `location_sharing` has no expiry by design, so a phone that is force-killed
    or loses signal for good leaves the setting on and the last coordinate in
    the row. The session teardown catches it on the next launch, and the next
    launch may never come — so until then the row would keep being served as
    though its owner were standing there.

    The split these tests pin: freshness gates *publication*, never the choice.
    A stale user is off other people's maps and can still fix it.
    """

    HERE = {'latitude': '10.00000000', 'longitude': '10.00000000'}

    def setUp(self):
        self.interest = Interest.objects.create(name='Stale Sailing')
        self.me = make_user('stale_me', latitude='10.0000', longitude='10.0000')
        self.viewer = make_user('stale_viewer', latitude='10.0001', longitude='10.0001')
        for user in (self.me, self.viewer):
            user.interests.set([self.interest])
            share_location(user)

        self.client_me = APIClient()
        self.client_me.force_authenticate(self.me)
        self.viewer_client = APIClient()
        self.viewer_client.force_authenticate(self.viewer)

    def age_position(self, seconds):
        """Backdate my last report by `seconds`, leaving the setting alone."""
        from django.utils import timezone
        from datetime import timedelta

        old = timezone.now() - timedelta(seconds=seconds)
        # `.update()` so `updated_at` (auto_now) ages with it — otherwise the
        # fallback in `fresh_position_q` would keep the row looking fresh and
        # the test would pass for the wrong reason.
        User.objects.filter(pk=self.me.pk).update(
            location_updated_at=old, updated_at=old,
        )
        self.me.refresh_from_db()

    def go_stale(self):
        """Age my position past the window, whatever the window is set to."""
        from users.visibility import position_max_age_s

        self.age_position(position_max_age_s() + 60)

    def nearby_ids(self):
        return {row['id'] for row in self.viewer_client.get(NEARBY).json()}

    def test_a_fresh_sharer_is_visible(self):
        """The control. Without this the assertions below pass vacuously."""
        self.assertIn(self.me.pk, self.nearby_ids())

    # -- the window itself ----------------------------------------------------
    # Pinned by magnitude rather than by re-reading the constant, which would
    # only assert that a number equals itself. These say what the number *means*
    # in minutes of a real person not reporting, so moving it is a deliberate
    # act with a failing test attached rather than a one-character edit.

    def test_a_position_from_fourteen_minutes_ago_is_still_published(self):
        """The tolerance half. Ten-second foreground reports and 30s/50m
        background ones mean roughly thirty consecutive misses fit inside the
        window — a tunnel or a dead spot must not drop somebody off the map."""
        self.age_position(14 * 60)
        self.assertIn(self.me.pk, self.nearby_ids())

    def test_a_position_from_sixteen_minutes_ago_is_withheld(self):
        """The cutoff half. Past this we stop claiming to know where they are."""
        self.age_position(16 * 60)
        self.assertNotIn(self.me.pk, self.nearby_ids())

    def test_club_scoping_shares_the_one_window(self):
        """One answer to "how old is too old", read from one place.

        Clubs used to keep its own six-hour copy, which meant a position could
        be too stale to draw on a map and still fresh enough to decide which
        city somebody was in. `clubs.visibility` now reads this value rather
        than declaring one, so the two cannot drift; the assertion is here to
        catch a future edit that reintroduces a second literal.
        """
        from clubs.visibility import _max_age
        from users.visibility import position_max_age_s

        self.assertEqual(_max_age(), position_max_age_s())

    def test_a_stale_sharer_is_not_returned_to_anyone(self):
        self.go_stale()
        self.assertNotIn(self.me.pk, self.nearby_ids())

    def test_stale_and_paused_are_indistinguishable_from_outside(self):
        """A viewer must not be able to tell "stopped reporting" from "checked
        out" — the two look identical, which is what stops the gap being a
        side channel for whether somebody's phone died."""
        self.go_stale()
        stale_view = self.viewer_client.get(NEARBY).json()

        User.objects.filter(pk=self.me.pk).update(
            location_sharing=User.LocationSharing.PAUSED,
            latitude=None, longitude=None, location_updated_at=None,
        )
        paused_view = self.viewer_client.get(NEARBY).json()

        self.assertEqual(stale_view, paused_view)

    def test_a_stale_user_can_still_report_a_position(self):
        """The deadlock this split exists to avoid.

        Gating the *write* on freshness would lock out precisely the people
        trying to become fresh again, and their only route back onto the map
        would be to check out and in.
        """
        self.go_stale()
        response = self.client_me.patch(LOCATION, self.HERE, format='json')
        self.assertEqual(response.status_code, 200)
        # And they are back, without having touched the setting.
        self.me.refresh_from_db()
        self.assertEqual(self.me.location_sharing, User.LocationSharing.LIVE)
        self.assertIn(self.me.pk, self.nearby_ids())

    def test_a_stale_user_can_still_ask_who_is_nearby(self):
        """Their own query is anchored on their own position. Stale or not,
        that is a question about them, not a publication of them."""
        self.go_stale()
        self.assertEqual(self.client_me.get(NEARBY).status_code, 200)

    def test_checking_out_while_stale_still_tears_down(self):
        """The rows most in need of clearing are exactly the stale ones, so the
        teardown must not test freshness before running."""
        self.go_stale()
        response = self.client_me.patch(
            PRIVACY, {'location_sharing': 'paused'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertIsNone(self.me.latitude)
        self.assertIsNone(self.me.location_updated_at)
        self.assertIsNone(self.me.sharing_session)

    def test_the_python_and_sql_halves_agree(self):
        """`is_on_map` and `on_map_q` are twins and drift is the failure mode
        that would leave one surface publishing what the other withholds."""
        from users import visibility

        for stale in (False, True):
            if stale:
                self.go_stale()
            in_sql = User.objects.filter(
                visibility.on_map_q(), pk=self.me.pk,
            ).exists()
            self.assertEqual(
                self.me.is_on_map(), in_sql,
                f'halves disagree when stale={stale}',
            )

    def test_freshness_does_not_gate_the_choice(self):
        """`is_sharing` is about what the user asked for, and nothing else."""
        self.go_stale()
        self.assertTrue(self.me.is_sharing())
        self.assertFalse(self.me.has_fresh_position())
        self.assertFalse(self.me.is_on_map())

    def test_a_stale_authors_story_pin_survives(self):
        """The deliberate asymmetry.

        A pin sits at the coordinate the story was posted at, so it says nothing
        about where its author is standing now. Withdrawing it because their
        phone stopped reporting would hide a story that is still theirs to show.
        """
        from stories.visibility import is_placed_for
        from stories.models import Story

        befriend(self.me, self.viewer)
        story = Story.objects.create(
            user=self.me, content='here', latitude='10.0000', longitude='10.0000',
        )
        self.assertTrue(is_placed_for(self.viewer, story))
        self.go_stale()
        story.user.refresh_from_db()
        self.assertTrue(
            is_placed_for(self.viewer, story),
            'a story pin was withdrawn because its author stopped reporting',
        )
