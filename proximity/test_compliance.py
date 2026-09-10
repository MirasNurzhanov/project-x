"""The privacy promises the API makes, asserted end to end.

These overlap with the other proximity suites on purpose: those test units,
this tests the promises, and a promise that survives a refactor of the unit
under it is the thing worth keeping.

This file used to assert a different product. Sharing was a per-launch
decision — a mode chosen against a consent sheet, torn down again by the next
cold start — and most of what was here pinned that teardown from every angle.
Sharing now follows the OS permission and persists, the way Find My and Snap
Map do, so those assertions were deleted rather than adjusted: they described
behaviour that is deliberately gone, and a test kept limping past a product
decision is worse than no test.

What replaced them is the pair of promises the new model actually makes:

    precision follows the relationship   friends get the point, everybody
                                         else gets a ~500m cell, and the
                                         distance agrees with the pin
    Ghost Mode is mutual                 a ghost is on nobody's map and has
                                         nobody on theirs, and it still has
                                         its clubs

The three states the whole API turns on:

    ghosted                  200 []          nothing to show, working as chosen
    sharing, no position     400 + code      transient; ask again with a fix
    sharing, with a position 200 [rows]      the normal answer
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


class ApiStateMatrixTests(TestCase):
    """One test per row of the table above, plus the codes they carry."""

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def test_ghosted_is_an_empty_list_not_an_error(self):
        self.me.ghost_mode = True
        self.me.save(update_fields=['ghost_mode'])
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

    def test_a_ghost_may_still_report_a_position(self):
        """Ghost Mode is about the map, not about the app.

        The write used to be refused outright, which also emptied the ghost's
        club list and region, because those resolve from the same stored
        position. Ghosting hides you from other people; it does not take the
        product away from you. `visible_users` is what keeps the position
        unpublished — see `test_ghost_is_mutual`.
        """
        self.me.ghost_mode = True
        self.me.save(update_fields=['ghost_mode'])
        response = self.client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertIsNotNone(self.me.latitude)

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
    """What arriving does, and what it still does not do."""

    def test_a_new_account_shares_but_has_no_position_yet(self):
        """Not ghosted, and not on the map either.

        The default flipped: sharing follows the OS permission now rather than
        an in-app choice, so a fresh row is willing to be seen. It is still
        absent from every map until a position arrives, which is the half that
        did not change and the half that does the work on a cold install.
        """
        user = make_user('fresh')
        self.assertFalse(user.ghost_mode)
        self.assertIsNone(user.latitude)
        self.assertFalse(user.is_on_map())

    def test_browsing_alone_still_publishes_nothing(self):
        """Reading the map is a read.

        Sharing is automatic now, but *being on* the map still takes a position
        this user reported, and the private query deliberately reports nothing.
        Somebody who only ever browses stays off it.
        """
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


class GhostModeTests(TestCase):
    """The one off switch, and exactly how much it takes with it."""

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

    def test_ghosting_takes_you_off_every_map(self):
        response = self.client.patch(PRIVACY, {'ghost_mode': True}, format='json')
        self.assertEqual(response.status_code, 200)

        self.me.refresh_from_db()
        self.assertFalse(self.me.is_on_map())

        watcher = share_location(make_user('watcher', latitude='10.0', longitude='10.0'))
        befriend(watcher, self.me)
        other = APIClient()
        other.force_authenticate(watcher)
        seen = {row['id'] for row in other.get(NEARBY).json()}
        self.assertNotIn(self.me.pk, seen)

    def test_ghost_is_mutual(self):
        """Opting out of being seen is opting out of seeing.

        Half privacy and half fairness: a one-way ghost watches a map it has
        removed itself from.
        """
        neighbour = share_location(
            make_user('neighbour', latitude='10.0', longitude='10.0'))
        neighbour_client = APIClient()
        neighbour_client.force_authenticate(neighbour)
        neighbour_client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json')

        # Visible to each other first, or the assertion below proves nothing.
        self.assertIn(
            neighbour.pk, {row['id'] for row in self.client.get(NEARBY).json()})

        self.client.patch(PRIVACY, {'ghost_mode': True}, format='json')
        self.assertEqual(self.client.get(NEARBY).json(), [])

    def test_ghosting_keeps_the_position_it_stops_publishing(self):
        """The narrow promise, and the reason this mode exists.

        Pausing used to wipe the row, which also emptied the user's own club
        list and region — they resolve from the same coordinate. Ghosting
        leaves it, and `visible_users` is what keeps it unpublished.
        """
        self.client.patch(PRIVACY, {'ghost_mode': True}, format='json')

        self.me.refresh_from_db()
        self.assertIsNotNone(self.me.latitude)
        self.assertIsNotNone(self.me.longitude)
        self.assertIsNotNone(self.me.location_updated_at)

    def test_ghosting_survives_a_new_session(self):
        """It is a setting, not a per-run state — in both directions.

        Sharing persisting across launches is the headline change; a ghost
        being un-ghosted by reopening the app would be the same bug wearing the
        opposite sign.
        """
        self.client.patch(PRIVACY, {'ghost_mode': True}, format='json')

        again = APIClient()
        again.force_authenticate(self.me)
        self.assertTrue(again.get(PRIVACY).json()['ghost_mode'])
        self.assertEqual(again.get(NEARBY).json(), [])

    def test_sharing_survives_a_new_session(self):
        """The headline change, asserted from the outside.

        A cold launch used to end the previous session and leave the user
        paused, so this is the assertion that would fail if any of that came
        back.
        """
        again = APIClient()
        again.force_authenticate(self.me)
        self.assertFalse(again.get(PRIVACY).json()['ghost_mode'])

        self.me.refresh_from_db()
        self.assertTrue(self.me.is_on_map())


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
        self.a.ghost_mode = True
        self.a.save(update_fields=['ghost_mode'])

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


class GhostIsOnlyChangedDeliberatelyTests(TestCase):
    """Nothing on the server flips Ghost Mode except a request that asks it to.

    This class used to be `NoAutomaticVisibilityTests`, and it asserted the
    opposite of the current product: that no event could ever make somebody
    visible, because visibility had to be chosen against a consent sheet each
    run. Sharing follows the OS permission now, so that premise is gone.

    What survives is the narrower and still-worth-having half: `ghost_mode` has
    exactly one writable route, so a user who has turned themselves off cannot
    be turned back on by a side effect of using the app. The direction of the
    danger reversed, the shape of the guard did not.
    """

    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.me.ghost_mode = True
        self.me.save(update_fields=['ghost_mode'])
        self.client.force_authenticate(self.me)

    def assert_still_ghosted(self, note):
        self.me.refresh_from_db()
        self.assertTrue(self.me.ghost_mode, note)
        self.assertFalse(self.me.is_on_map(), note)

    def test_reporting_a_position_does_not_unghost(self):
        for _ in range(3):
            self.client.patch(
                LOCATION, {'latitude': '10.0', 'longitude': '10.0', 'speed': 3.0},
                format='json',
            )
        self.assert_still_ghosted('a location report cleared Ghost Mode')

    def test_browsing_the_map_does_not_unghost(self):
        for _ in range(3):
            self.client.post(
                NEARBY_QUERY, {'latitude': '10.0', 'longitude': '10.0'},
                format='json',
            )
            self.client.get(NEARBY)
        self.assert_still_ghosted('browsing cleared Ghost Mode')

    def test_reading_the_privacy_screen_does_not_unghost(self):
        self.client.get(PRIVACY)
        self.assert_still_ghosted('reading the privacy screen cleared Ghost Mode')

    def test_other_privacy_settings_do_not_drag_ghost_with_them(self):
        self.client.patch(PRIVACY, {'is_private': True}, format='json')
        self.assert_still_ghosted('an unrelated privacy write cleared Ghost Mode')

    def test_a_zone_change_does_not_unghost(self):
        self.client.patch(
            '/api/users/zone/', {'zone_active': True}, format='json',
        )
        self.assert_still_ghosted('a zone change cleared Ghost Mode')

    def test_the_privacy_endpoint_is_the_one_that_can(self):
        """The other side of every assertion above. A field nothing can write
        is not protected, it is broken."""
        response = self.client.patch(PRIVACY, {'ghost_mode': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertFalse(self.me.ghost_mode)

    def test_the_serializer_is_the_only_place_the_field_is_writable(self):
        """Static, so it also covers the endpoint nobody has written yet."""
        from rest_framework import serializers as drf

        from users import serializers as user_serializers

        writable = []
        for name in dir(user_serializers):
            attr = getattr(user_serializers, name)
            if not isinstance(attr, type) or not issubclass(attr, drf.BaseSerializer):
                continue
            meta = getattr(attr, 'Meta', None)
            fields = getattr(meta, 'fields', ()) or ()
            read_only = getattr(meta, 'read_only_fields', ()) or ()
            if 'ghost_mode' in fields and 'ghost_mode' not in read_only:
                writable.append(name)

        self.assertEqual(writable, ['PrivacySettingsSerializer'])


class PrecisionFollowsTheRelationshipTests(TestCase):
    """Friends get the point. Everybody else gets a ~500m cell.

    The promise that replaced the Live/Approximate setting, and the one that
    the storage inversion makes load-bearing: the row holds the precise
    position now, so these are the assertions standing between it and a
    stranger.
    """

    def setUp(self):
        self.client = APIClient()
        self.target = share_location(make_user('target'))
        reporter = APIClient()
        reporter.force_authenticate(self.target)
        reporter.patch(
            LOCATION, {'latitude': '10.00000000', 'longitude': '10.00000000'},
            format='json',
        )
        self.target.refresh_from_db()

    def row_for(self, viewer):
        client = APIClient()
        client.force_authenticate(viewer)
        client.patch(
            LOCATION, {'latitude': '10.0', 'longitude': '10.0'}, format='json')
        rows = {r['id']: r for r in client.get(NEARBY).json()}
        return rows.get(self.target.pk)

    def test_the_row_itself_is_precise(self):
        """Otherwise the two tests below would agree for the wrong reason."""
        self.assertEqual(float(self.target.latitude), 10.0)

    def test_a_friend_gets_the_point(self):
        friend = share_location(make_user('friend'))
        befriend(friend, self.target)
        row = self.row_for(friend)
        self.assertEqual(row['location_precision'], 'precise')
        self.assertAlmostEqual(float(row['latitude']), 10.0, places=7)

    def test_a_stranger_gets_a_cell(self):
        row = self.row_for(share_location(make_user('stranger')))
        self.assertEqual(row['location_precision'], 'approximate')
        self.assertNotAlmostEqual(float(row['latitude']), 10.0, places=5)

    def test_a_strangers_distance_agrees_with_the_pin_they_were_given(self):
        """A coarse pin beside a precise distance is a precise pin.

        Two or three exact distances from known vantage points trilaterate the
        point the cell was meant to hide, so the number has to be measured to
        the cell rather than to the truth.
        """
        from proximity.utils import haversine

        row = self.row_for(share_location(make_user('measurer')))
        expected = haversine(10.0, 10.0, float(row['latitude']), float(row['longitude']))
        self.assertAlmostEqual(row['distance_km'], round(expected, 2), places=2)


class StoryLocationTests(TestCase):
    """Stories and live location are two products that share one table.

    The rule the app promises, and the one asserted here:

        Ghost Mode takes you and your stories off other people's maps and
        hides your profile content from everyone else, including friends.

    So every test below is one of two questions asked of the same row - may my
    friend read this, and may anybody draw it at a coordinate - and the point
    is that they have different answers. See stories/visibility.py.
    """

    STORIES = '/api/stories/'
    NEARBY_STORIES = '/api/stories/nearby/'
    FRIEND_STORIES = '/api/stories/friends/'
    MAP_QUERY = '/api/map/nearby/query/'
    MINE = '/api/stories/mine/'

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
        response = client.patch(PRIVACY, {'ghost_mode': True}, format='json')
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

    def test_a_ghosted_story_is_hidden_from_a_friend(self):
        self.pause(self.author_client)
        story_id = self.post_story().json()['id']
        self.assertNotIn(story_id, self.feed_story_ids())

    def test_a_paused_story_is_not_on_the_map(self):
        # The viewer is still Live, and their own pin is the control: without
        # it "absent from the composite map" also passes against a map that
        # returned nothing at all.
        control_id = self.post_story(self.viewer_client).json()['id']
        self.pause(self.author_client)
        story_id = self.post_story().json()['id']

        self.assertNotIn(story_id, self.map_story_ids())
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
        self.assertEqual(rows, [])

    # -- pausing after posting ------------------------------------------------
    def test_ghosting_retracts_the_pin_and_the_publication(self):
        """The case a write-time check could never have handled.

        Nothing was wrong when this story was posted - the author was on the
        map. Ghosting changes both answers; the author retains the saved row.
        """
        story_id = self.post_story().json()['id']
        self.assertIn(story_id, self.map_story_ids())

        self.pause(self.author_client)
        self.assertNotIn(story_id, self.map_story_ids())
        self.assertNotIn(story_id, self.feed_story_ids())

    def test_resuming_brings_the_pin_back(self):
        """Pausing retracts, it does not destroy. Otherwise the switch is a
        one-way door and users learn not to touch it."""
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)
        self.assertNotIn(story_id, self.map_story_ids())

        self.author_client.patch(PRIVACY, {'ghost_mode': False}, format='json')
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
    def test_a_friend_cannot_react_when_the_author_goes_anonymous(self):
        """A saved story ID must not bypass the anonymous profile gate."""
        story_id = self.post_story().json()['id']
        self.pause(self.author_client)

        response = self.viewer_client.post(
            f'/api/stories/{story_id}/react/', {'reaction': 'like'}, format='json',
        )
        self.assertEqual(response.status_code, 404, response.content)

    def test_a_ghosted_pair_cannot_react_to_each_others_hidden_stories(self):
        """Hiding one's own profile does not grant access to another's."""
        self.pause(self.author_client)
        self.pause(self.viewer_client)
        story_id = self.post_story().json()['id']

        response = self.viewer_client.post(
            f'/api/stories/{story_id}/react/', {'reaction': 'like'}, format='json',
        )
        self.assertEqual(response.status_code, 404, response.content)

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
    def test_a_friend_sees_the_story_pin_precisely(self):
        """Stories are friends-only to begin with, so this is the case that
        actually happens: the audience for a pin is the audience entitled to
        the precise point, and it gets it."""
        story_id = self.post_story().json()['id']
        row = next(
            s for s in self.viewer_client.get('/api/stories/nearby/').json()
            if s['id'] == story_id
        )

        self.assertAlmostEqual(row['latitude'], 10.0, places=6)
        self.assertAlmostEqual(row['longitude'], 10.0, places=6)

    def test_the_serializer_still_coarsens_for_a_non_friend(self):
        """Defence in depth, asserted at the serializer.

        No endpoint currently shows a story to a non-friend — `placed_q` gates
        on friendship well before this — so this is the one assertion in the
        file with no route to it through the API. It is here because the pin is
        stored precisely now, and "the audience filter upstream happens to be
        strict" is the kind of guarantee that quietly stops being true when the
        audience widens.
        """
        from rest_framework.test import APIRequestFactory

        from stories.models import Story
        from stories.serializers import StorySerializer
        from users.visibility import snap_coordinates

        story_id = self.post_story().json()['id']
        stranger = share_location(make_user('story-stranger'))
        request = APIRequestFactory().get('/')
        request.user = stranger

        row = StorySerializer(
            Story.objects.get(pk=story_id), context={'request': request},
        ).data

        snapped_lat, _ = snap_coordinates(self.author, 10.0, 10.0)
        self.assertNotAlmostEqual(row['latitude'], 10.0, places=5)
        self.assertAlmostEqual(row['latitude'], float(snapped_lat), places=6)


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

    def test_stale_and_ghosted_are_indistinguishable_from_outside(self):
        """A viewer must not be able to tell "stopped reporting" from "turned
        themselves off" — the two look identical, which is what stops the gap
        being a side channel for whether somebody's phone died."""
        self.go_stale()
        stale_view = self.viewer_client.get(NEARBY).json()

        User.objects.filter(pk=self.me.pk).update(ghost_mode=True)
        ghosted_view = self.viewer_client.get(NEARBY).json()

        self.assertEqual(stale_view, ghosted_view)

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
        self.assertFalse(self.me.ghost_mode)
        self.assertIn(self.me.pk, self.nearby_ids())

    def test_a_stale_user_can_still_ask_who_is_nearby(self):
        """Their own query is anchored on their own position. Stale or not,
        that is a question about them, not a publication of them."""
        self.go_stale()
        self.assertEqual(self.client_me.get(NEARBY).status_code, 200)

    def test_ghosting_while_stale_takes_them_off_the_map(self):
        """Ghosting is a setting, not a teardown.

        This used to assert the position was wiped. It is kept now — a ghost
        still needs it for their own clubs and region — so what is asserted is
        the part that matters to everybody else: they are gone from the map,
        stale or not.
        """
        self.go_stale()
        response = self.client_me.patch(
            PRIVACY, {'ghost_mode': True}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.me.refresh_from_db()
        self.assertTrue(self.me.ghost_mode)
        self.assertFalse(self.me.is_on_map())
        self.assertNotIn(self.me.pk, self.nearby_ids())
        # Still theirs to use.
        self.assertIsNotNone(self.me.latitude)

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
