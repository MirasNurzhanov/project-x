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
