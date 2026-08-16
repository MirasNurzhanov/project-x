"""Browsing the map while paused, without becoming visible by doing it.

Two paths reach the same discovery logic and they must stay different in
exactly one respect:

    a public location update requires sharing to be on
    a private nearby query does not, and never turns it on

Most of what follows is therefore negative: it asserts what the private path
does *not* write. That is the point — the endpoint is safe because it has no
side effects, and a test suite that only checked the returned rows would still
pass on the day somebody added one.
"""
import ast
import inspect

from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from encounters.models import Encounter
from interests.models import Interest
from notifications.models import Notification
from users.middleware import HEADER, FOREGROUND, PRESENCE_EXEMPT_PATHS
from users.models import User
from users.testing import share_location

QUERY_URL = '/api/proximity/nearby/query/'
MAP_QUERY_URL = '/api/map/nearby/query/'


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


class PausedBrowsingTests(TestCase):
    """A paused user can see, and cannot be seen."""

    def setUp(self):
        self.client = APIClient()
        # Paused by default, and deliberately given no stored position: this is
        # the state a paused user is actually in after users/0029.
        self.me = make_user('me')
        self.neighbour = share_location(
            make_user('neighbour', latitude='10.0005', longitude='10.0005'))
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.neighbour.interests.add(interest)
        self.client.force_authenticate(self.me)

    def query(self, lat='10.0', lon='10.0'):
        response = self.client.post(
            QUERY_URL, {'latitude': lat, 'longitude': lon}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_a_paused_user_can_browse(self):
        self.assertIn(self.neighbour.pk, {row['id'] for row in self.query().json()})

    def test_the_stored_route_still_returns_nothing_for_them(self):
        """The two paths are not interchangeable, and the GET is the one that
        needs an anchor it does not have.

        Empty rather than an error, and deliberately unlike the not-yet-
        reported case below: being paused is a working state, not a failure.
        """
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_a_sharing_user_with_no_position_yet_gets_a_400(self):
        """The gap right after choosing Live, which the client falls back on.

        The two empty-handed cases have to be distinguishable: an empty list
        means "nobody is around" and the map draws it, a 400 means "ask again
        with your own coordinate".
        """
        share_location(self.me)
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 400)

    def test_browsing_stores_no_position(self):
        self.query()
        self.me.refresh_from_db()
        self.assertIsNone(self.me.latitude)
        self.assertIsNone(self.me.longitude)
        self.assertIsNone(self.me.location_updated_at)
        self.assertIsNone(self.me.location_accuracy_m)

    def test_browsing_does_not_start_sharing(self):
        self.query()
        self.me.refresh_from_db()
        self.assertEqual(self.me.location_sharing, User.LocationSharing.PAUSED)
        self.assertFalse(self.me.is_on_map())

    def test_browsing_does_not_touch_motion(self):
        self.query()
        self.me.refresh_from_db()
        self.assertEqual(self.me.motion_class, User.MotionClass.STATIONARY)
        self.assertIsNone(self.me.motion_updated_at)

    def test_browsing_records_no_encounter(self):
        """An encounter says two people were in the same place. The browser was
        not published, so as far as everyone else is concerned they were not
        there at all."""
        self.query()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_browsing_announces_nobody(self):
        self.query()
        self.assertFalse(
            Notification.objects.filter(
                notification_type=Notification.Type.NEARBY_USER,
            ).exists(),
        )

    def test_browsing_leaves_the_browser_invisible(self):
        """The whole point, checked from the other side of the glass."""
        self.query()

        other = APIClient()
        other.force_authenticate(self.neighbour)
        response = other.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.me.pk, {row['id'] for row in response.json()})

    def test_browsing_creates_no_presence(self):
        """Not even the heartbeat that every other foreground request writes."""
        self.assertIn(QUERY_URL, PRESENCE_EXEMPT_PATHS)

        self.client.post(
            QUERY_URL, {'latitude': '10.0', 'longitude': '10.0'},
            format='json', **{HEADER.replace('HTTP_', '').replace('_', '-'): FOREGROUND},
        )
        self.me.refresh_from_db()
        self.assertIsNone(self.me.last_seen)

    def test_a_paused_user_still_cannot_write_a_position(self):
        """The protection this feature had to be built around, not through."""
        response = self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.assertEqual(response.status_code, 403)


class QueryAnchorTests(TestCase):
    """The supplied coordinate is what the search is measured from."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.me.interests.add(Interest.objects.create(name='Climbing'))
        self.client.force_authenticate(self.me)

    def near(self, name, lat, lon):
        user = share_location(make_user(name, latitude=str(lat), longitude=str(lon)))
        user.interests.set(self.me.interests.all())
        return user

    def ids_from(self, lat, lon):
        response = self.client.post(
            QUERY_URL, {'latitude': str(lat), 'longitude': str(lon)}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()}

    def test_the_anchor_decides_who_is_in_range(self):
        here = self.near('here', 10.0, 10.0)
        elsewhere = self.near('elsewhere', 40.0, 40.0)

        self.assertEqual(self.ids_from(10.0, 10.0), {here.pk})
        self.assertEqual(self.ids_from(40.0, 40.0), {elsewhere.pk})

    def test_the_zone_dial_still_governs_reach(self):
        """Reusing the existing geometry, not a second copy of it: the viewer's
        own zone decides how far they see, exactly as on the stored path."""
        self.me.zone_radius = User.ZONE_RADIUS_MIN_M
        self.me.save(update_fields=['zone_radius'])
        far = self.near('far', 10.004, 10.0)  # ~440m north
        self.assertNotIn(far.pk, self.ids_from(10.0, 10.0))

        self.me.zone_radius = 5000
        self.me.save(update_fields=['zone_radius'])
        self.assertIn(far.pk, self.ids_from(10.0, 10.0))

    def test_paused_people_are_not_returned_to_a_private_browser_either(self):
        """Browsing privately does not buy a wider view than browsing openly."""
        hidden = make_user('hidden', latitude='10.0005', longitude='10.0005')
        hidden.interests.set(self.me.interests.all())
        self.assertNotIn(hidden.pk, self.ids_from(10.0, 10.0))

    def test_coordinates_are_required_and_validated(self):
        for body in ({}, {'latitude': '10.0'}, {'latitude': '95.0', 'longitude': '10.0'}):
            response = self.client.post(QUERY_URL, body, format='json')
            self.assertEqual(response.status_code, 400, body)

    def test_the_query_takes_no_field_that_would_be_stored(self):
        """`accuracy` and `speed` are update-endpoint concepts. Accepting them
        here would be leaving a door open for a future writer."""
        response = self.client.post(
            QUERY_URL,
            {'latitude': '10.0', 'longitude': '10.0', 'accuracy': 5.0, 'speed': 12.0},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertIsNone(self.me.location_accuracy_m)
        self.assertIsNone(self.me.motion_updated_at)


class MapQueryTests(TransactionTestCase):
    """The composite map, same anchor rules.

    `TransactionTestCase` because this view fans its three reads across a
    thread pool, and SQLite refuses concurrent access to tables held inside the
    single open transaction `TestCase` wraps each test in. Production is
    Postgres; the test has to commit to be able to observe anything.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.neighbour = share_location(
            make_user('neighbour', latitude='10.0005', longitude='10.0005'))
        interest = Interest.objects.create(name='Climbing')
        self.me.interests.add(interest)
        self.neighbour.interests.add(interest)
        self.client.force_authenticate(self.me)

    def test_it_answers_a_paused_caller(self):
        response = self.client.post(
            MAP_QUERY_URL, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn('users', body)
        self.assertIn('stories', body)
        self.assertIn('meetups', body)

    def test_it_reports_a_place_without_recording_one(self):
        """A paused user can still tell which city they are in, and no
        UserPlace row is written to tell them."""
        from regions.models import UserPlace

        response = self.client.post(
            MAP_QUERY_URL, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('place', response.json())
        self.assertFalse(UserPlace.objects.filter(user=self.me).exists())

    def test_the_stored_map_route_refuses_a_paused_caller(self):
        response = self.client.get('/api/map/nearby/')
        self.assertEqual(response.status_code, 400)

    def test_it_writes_nothing_about_the_caller(self):
        self.client.post(
            MAP_QUERY_URL, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.me.refresh_from_db()
        self.assertIsNone(self.me.latitude)
        self.assertEqual(self.me.location_sharing, User.LocationSharing.PAUSED)
        self.assertEqual(Encounter.objects.count(), 0)

    def test_the_exempt_path_list_matches_the_route(self):
        self.assertIn(MAP_QUERY_URL, PRESENCE_EXEMPT_PATHS)


class SharingUserTests(TestCase):
    """The other half of the table: nothing above weakened the public path."""

    def setUp(self):
        self.client = APIClient()
        self.me = share_location(make_user('me'))
        self.client.force_authenticate(self.me)

    def test_a_sharing_user_still_stores_their_position(self):
        response = self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0', 'accuracy': 8.0},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertEqual(float(self.me.latitude), 10.0)
        self.assertIsNotNone(self.me.location_updated_at)
        self.assertEqual(self.me.location_accuracy_m, 8.0)

    def test_the_private_query_does_not_disturb_a_stored_position(self):
        """Open to a sharing caller, and still inert for them: the anchor it is
        given is used for the answer and thrown away, not written over the
        position other people are being shown."""
        self.client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0', 'longitude': '10.0'},
            format='json',
        )
        self.client.post(
            QUERY_URL, {'latitude': '40.0', 'longitude': '40.0'}, format='json',
        )
        self.me.refresh_from_db()
        self.assertEqual(float(self.me.latitude), 10.0)


class NoWritesOnTheReadPathTests(TestCase):
    """The private query is safe because it has no side effects. Pinned.

    Read statically rather than by exercising it, because the dangerous version
    of this bug is a write on a branch the tests happen not to take — a
    notification for one relationship tier, a place snapshot only when the
    cache misses. Walking the source catches those; calling the endpoint twice
    does not.

    The list of names is deliberately the *shape* of a write rather than a
    known set of callers, so a new one has to be looked at rather than
    inherited.
    """

    #: Django's write verbs, plus the app's own side-effecting helpers.
    WRITE_ATTRS = frozenset({
        'save', 'delete', 'bulk_create', 'bulk_update', 'get_or_create',
        'update_or_create',
    })
    WRITE_CALLS = frozenset({
        'record_encounters', 'notify_nearby_matches', 'notify_zone_music_entry',
        'refresh_quietly', 'snapshot_for', 'save_snapshot',
    })

    def offending_calls(self, fn):
        """Write-shaped calls in `fn`, ignoring local collections.

        `seen.add(...)` and `nearby.append(...)` are a set and a list held for
        the length of one request; `.create(...)` on a manager is not. The
        difference is the receiver, so bare local names are excused and
        anything reached through a manager or queryset is not.
        """
        tree = ast.parse(inspect.getsource(fn))
        local_names = {'seen', 'nearby', 'distances', 'matches', 'allowed'}
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                receiver = func.value
                is_local = isinstance(receiver, ast.Name) and receiver.id in local_names
                if func.attr in self.WRITE_ATTRS and not is_local:
                    found.append(f'{func.attr}() at line {node.lineno}')
                # `.create`/`.update`/`.add` are only writes off a manager.
                if func.attr in {'create', 'update', 'add', 'set', 'remove'} and not is_local:
                    found.append(f'{func.attr}() at line {node.lineno}')
            elif isinstance(func, ast.Name) and func.id in self.WRITE_CALLS:
                found.append(f'{func.id}() at line {node.lineno}')
        return found

    def test_nearby_users_writes_nothing(self):
        from proximity.views import nearby_users

        self.assertEqual(
            self.offending_calls(nearby_users), [],
            'A write appeared on the shared discovery path. It runs for paused '
            'users through /api/proximity/nearby/query/, which must not store '
            'a position, mark presence, or make the caller visible.',
        )

    def test_nearby_stories_writes_nothing(self):
        from stories.views import nearby_stories

        self.assertEqual(self.offending_calls(nearby_stories), [])

    def test_the_transient_place_lookup_never_reaches_upstream(self):
        """Cache-only, which is also what keeps coordinates out of the logs:
        the boundary warnings that print a point all live on the upstream
        path."""
        from proximity.views import _transient_place

        source = inspect.getsource(_transient_place)
        self.assertIn('allow_upstream=False', source)
