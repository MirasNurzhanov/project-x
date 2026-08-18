"""End-to-end privacy checks.

These play out the whole flow the way the app does — update location, poll
nearby, read the feeds — instead of asserting on one endpoint in isolation.
Leaks in this system come from the surfaces nobody thought to filter (an old
notification, a stale encounter, the reverse half of a two-way write), so the
question each test asks is "after doing the normal thing, does any endpoint
still name them".
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from encounters.models import Encounter
from friends.models import BlockedUser, FriendRequest, HiRequest
from interests.models import Interest
from notifications.models import Notification
from stories.models import Story
from users.models import User
from users.testing import share_location


def make_user(name, **extra):
    # On the map by default. `location_sharing` defaults to Paused now (see
    # users.visibility), so a fixture for a test about anything else has to opt
    # in or it measures the default instead of its own subject.
    # proximity/test_map_visibility.py owns the assertions about the default.
    return share_location(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    ))


class HidingMyLocationTests(TestCase):
    """"I don't want to be findable." Every route out of the system has to
    agree, including the ones that write to the other person's account."""

    def setUp(self):
        self.interest = Interest.objects.create(name='Test Kayaking')
        self.me = make_user('me', latitude='10.0000', longitude='10.0000')
        self.other = make_user('other', latitude='10.0010', longitude='10.0010')
        for user in (self.me, self.other):
            user.interests.set([self.interest])

        self.my_client = APIClient()
        self.my_client.force_authenticate(self.me)
        self.their_client = APIClient()
        self.their_client.force_authenticate(self.other)

    def ping(self, client, lat, lon):
        """A location update, exactly as the app sends it."""
        return client.patch(
            '/api/proximity/location/',
            {'latitude': str(lat), 'longitude': str(lon)},
            format='json',
        )

    def nearby_ids(self, client):
        response = client.get('/api/proximity/nearby/')
        if response.status_code != 200:
            return set()
        return {entry['id'] for entry in response.json()}

    # ── Paused ──────────────────────────────────────────────────────────────
    def test_paused_hides_me_from_others_even_when_they_move(self):
        self.me.location_sharing = User.LocationSharing.PAUSED
        self.me.save(update_fields=['location_sharing'])

        self.ping(self.their_client, '10.0010', '10.0010')
        self.assertNotIn(self.me.pk, self.nearby_ids(self.their_client))

    def test_paused_hides_others_from_me(self):
        self.me.location_sharing = User.LocationSharing.PAUSED
        self.me.save(update_fields=['location_sharing'])
        self.assertEqual(self.nearby_ids(self.my_client), set())

    def test_paused_produces_no_notification_from_either_side(self):
        self.me.location_sharing = User.LocationSharing.PAUSED
        self.me.save(update_fields=['location_sharing'])

        self.ping(self.my_client, '10.0000', '10.0000')
        self.ping(self.their_client, '10.0010', '10.0010')

        self.assertFalse(
            Notification.objects.filter(sender=self.me).exists(),
            'a paused user was announced to someone else',
        )
        self.assertFalse(
            Notification.objects.filter(recipient=self.me).exists(),
            'a paused user was told who is nearby',
        )

    def test_paused_produces_no_encounter(self):
        self.me.location_sharing = User.LocationSharing.PAUSED
        self.me.save(update_fields=['location_sharing'])

        self.ping(self.my_client, '10.0000', '10.0000')
        self.ping(self.their_client, '10.0010', '10.0010')
        self.assertFalse(Encounter.objects.exists())

    # ── Not discoverable by interest ────────────────────────────────────────
    def test_interest_opt_out_is_not_undone_by_the_other_persons_movement(self):
        """The reverse half of a two-way write is the easy thing to miss: they
        move, we are in *their* results, and the notification that fires names
        us back to them."""
        self.me.discoverable_by_interest = False
        self.me.save(update_fields=['discoverable_by_interest'])

        # Both sides move: mine matters most, because my own update is what
        # puts *them* in my results and fires the two-way write back at them.
        self.ping(self.my_client, '10.0000', '10.0000')
        self.ping(self.their_client, '10.0010', '10.0010')

        self.assertNotIn(self.me.pk, self.nearby_ids(self.their_client))
        self.assertFalse(
            Notification.objects.filter(recipient=self.other, sender=self.me).exists(),
            'opting out of interest discovery still announced me to a stranger',
        )

    def test_interest_opt_out_leaves_no_encounter_for_the_stranger(self):
        self.me.discoverable_by_interest = False
        self.me.save(update_fields=['discoverable_by_interest'])

        self.ping(self.my_client, '10.0000', '10.0000')
        self.ping(self.their_client, '10.0010', '10.0010')

        encounters = self.their_client.get('/api/encounters/').json()
        self.assertEqual(
            [e for e in encounters if e['other_user']['id'] == self.me.pk], [],
            'opting out of interest discovery still recorded me as an encounter',
        )

    def test_interest_opt_out_still_lets_friends_see_me(self):
        """Discoverability governs strangers finding you, not people you have
        already accepted."""
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other, status=FriendRequest.Status.ACCEPTED
        )
        self.me.discoverable_by_interest = False
        self.me.save(update_fields=['discoverable_by_interest'])

        self.ping(self.their_client, '10.0010', '10.0010')
        self.assertIn(self.me.pk, self.nearby_ids(self.their_client))


class HidingOtherPeopleTests(TestCase):
    """"I don't want to see them, and I don't want them seeing me." Blocking
    has to reach the records that already exist, not just future ones."""

    def setUp(self):
        self.interest = Interest.objects.create(name='Test Bouldering')
        self.me = make_user('me', latitude='10.0000', longitude='10.0000')
        self.pest = make_user('pest', latitude='10.0010', longitude='10.0010')
        for user in (self.me, self.pest):
            user.interests.set([self.interest])

        self.my_client = APIClient()
        self.my_client.force_authenticate(self.me)
        self.their_client = APIClient()
        self.their_client.force_authenticate(self.pest)

    def build_shared_history(self):
        """Get the pair into every list before the block lands."""
        for client, lat, lon in (
            (self.my_client, '10.0000', '10.0000'),
            (self.their_client, '10.0010', '10.0010'),
        ):
            client.patch(
                '/api/proximity/location/',
                {'latitude': lat, 'longitude': lon},
                format='json',
            )
        self.assertTrue(Encounter.objects.exists(), 'test setup produced no encounter')
        self.assertTrue(Notification.objects.exists(), 'test setup produced no notification')

    def block(self):
        return self.my_client.post(f'/api/friends/{self.pest.pk}/block/')

    def test_block_clears_them_from_my_encounters(self):
        self.build_shared_history()
        self.block()
        encounters = self.my_client.get('/api/encounters/').json()
        self.assertEqual(
            [e for e in encounters if e['other_user']['id'] == self.pest.pk], [],
            'a blocked user is still listed in encounters',
        )

    def test_block_clears_me_from_their_encounters(self):
        """Mutual invisibility: the person who was blocked must not keep a
        record of me either, or the disappearance itself is the tell."""
        self.build_shared_history()
        self.block()
        encounters = self.their_client.get('/api/encounters/').json()
        self.assertEqual(
            [e for e in encounters if e['other_user']['id'] == self.me.pk], [],
            'the blocked user still sees the blocker in encounters',
        )

    def test_block_clears_them_from_my_notifications(self):
        self.build_shared_history()
        self.block()
        notifications = self.my_client.get('/api/notifications/').json()
        results = notifications['results'] if isinstance(notifications, dict) else notifications
        self.assertEqual(
            [n for n in results if n['sender_username'] == 'pest'], [],
            'a blocked user is still named in notifications',
        )

    def test_block_clears_me_from_their_notifications(self):
        self.build_shared_history()
        self.block()
        notifications = self.their_client.get('/api/notifications/').json()
        results = notifications['results'] if isinstance(notifications, dict) else notifications
        self.assertEqual(
            [n for n in results if n['sender_username'] == 'me'], [],
            'the blocked user is still told about the blocker',
        )

    def test_block_removes_them_from_the_map_both_ways(self):
        self.build_shared_history()
        self.block()
        mine = {e['id'] for e in self.my_client.get('/api/proximity/nearby/').json()}
        theirs = {e['id'] for e in self.their_client.get('/api/proximity/nearby/').json()}
        self.assertNotIn(self.pest.pk, mine)
        self.assertNotIn(self.me.pk, theirs)

    def test_block_hides_stories_both_ways(self):
        # Friends first: stories are friends-only, so without this both lists
        # are empty before the block and the test cannot fail.
        FriendRequest.objects.create(
            sender=self.me, receiver=self.pest,
            status=FriendRequest.Status.ACCEPTED,
        )
        for user in (self.me, self.pest):
            Story.objects.create(
                user=user, content='hi', latitude='10.0005', longitude='10.0005',
                expires_at=timezone.now() + timezone.timedelta(hours=5),
            )
        self.assertTrue(self.my_client.get('/api/stories/nearby/').json())

        self.block()
        mine = self.my_client.get('/api/stories/nearby/').json()
        theirs = self.their_client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in mine if s['author']['id'] == self.pest.pk], [])
        self.assertEqual([s for s in theirs if s['author']['id'] == self.me.pk], [])
        # And out of the social feed as well, which the map filter does not
        # reach — blocking is not a map rule.
        feed = self.my_client.get('/api/stories/friends/').json()['results']
        self.assertEqual([s for s in feed if s['author']['id'] == self.pest.pk], [])

    def test_unblock_restores_visibility(self):
        self.build_shared_history()
        self.block()
        self.my_client.delete(f'/api/friends/{self.pest.pk}/unblock/')

        self.their_client.patch(
            '/api/proximity/location/',
            {'latitude': '10.0010', 'longitude': '10.0010'},
            format='json',
        )
        mine = {e['id'] for e in self.my_client.get('/api/proximity/nearby/').json()}
        self.assertIn(self.pest.pk, mine)


class ChokepointSafetyTests(TestCase):
    """The shared helpers have to be safe used alone. Every current caller
    filters blocks separately, so a regression here would stay invisible until
    some future endpoint used the helper on its own."""

    def setUp(self):
        self.me = make_user('me')
        self.pest = make_user('pest')
        # Friends, because a story only ever reaches a friend now (see
        # users.visibility.can_view). Without this the queryset is empty for
        # the right reason and the block assertion below proves nothing.
        FriendRequest.objects.create(
            sender=self.me, receiver=self.pest,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.story = Story.objects.create(
            user=self.pest, content='hi', latitude='10.0', longitude='10.0',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )

    def test_visible_field_q_excludes_blocked_authors_on_its_own(self):
        from users.visibility import visible_field_q

        self.assertTrue(Story.objects.filter(visible_field_q(self.me, 'stories')).exists())
        BlockedUser.objects.create(blocker=self.me, blocked=self.pest)
        self.assertFalse(
            Story.objects.filter(visible_field_q(self.me, 'stories')).exists(),
            'the shared story filter leaks blocked authors when used alone',
        )

    def test_visible_field_q_excludes_authors_who_blocked_me(self):
        from users.visibility import visible_field_q

        BlockedUser.objects.create(blocker=self.pest, blocked=self.me)
        self.assertFalse(Story.objects.filter(visible_field_q(self.me, 'stories')).exists())

    def test_visible_users_excludes_blocks_without_any_flags(self):
        from users.visibility import visible_users

        self.assertTrue(visible_users(self.me).filter(pk=self.pest.pk).exists())
        BlockedUser.objects.create(blocker=self.pest, blocked=self.me)
        self.assertFalse(visible_users(self.me).filter(pk=self.pest.pk).exists())


class PrivateAccountEndToEndTests(TestCase):
    """A private account still appears on the map — presence and profile
    privacy are separate axes — but carries nothing beyond a name and face."""

    def setUp(self):
        self.interest = Interest.objects.create(name='Test Surfing')
        self.me = make_user('me', latitude='10.0000', longitude='10.0000')
        self.shy = make_user('shy', latitude='10.0010', longitude='10.0010', is_private=True)
        for user in (self.me, self.shy):
            user.interests.set([self.interest])
        self.client = APIClient()
        self.client.force_authenticate(self.me)

    def test_private_user_is_on_the_map_but_carries_nothing(self):
        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertIn(self.shy.pk, entries)
        for field in ('bio', 'interests', 'shared_interests', 'zone_audio_url'):
            self.assertNotIn(field, entries[self.shy.pk])

    def test_hi_then_acceptance_opens_the_profile(self):
        """The existing Hi queue is the approval queue — no separate flow."""
        self.client.post('/api/friends/hi/', {'receiver_id': self.shy.pk}, format='json')

        their_client = APIClient()
        their_client.force_authenticate(self.shy)
        pending = their_client.get('/api/friends/requests/').json()
        self.assertEqual(len(pending), 1)
        their_client.post(f'/api/friends/hi/{pending[0]["id"]}/accept/')

        body = self.client.get(f'/api/users/profile/{self.shy.pk}/').json()
        self.assertIn('bio', body)
        self.assertIn('interests', body)

    def test_declined_request_leaves_the_profile_shut(self):
        self.client.post('/api/friends/hi/', {'receiver_id': self.shy.pk}, format='json')

        their_client = APIClient()
        their_client.force_authenticate(self.shy)
        pending = their_client.get('/api/friends/requests/').json()
        their_client.post(f'/api/friends/hi/{pending[0]["id"]}/decline/')

        body = self.client.get(f'/api/users/profile/{self.shy.pk}/').json()
        self.assertNotIn('bio', body)


class NoLocationSocketTests(TestCase):
    """There is no socket that reports a position or answers "who is nearby".

    There was one, and it hand-rolled its own payload and its own radius rather
    than going through the serializer and the zone engine — which is how it came
    to write a paused user's coordinates to their row before checking whether
    they were sharing at all, something the REST endpoint refuses with a 403.
    Nothing subscribed to it. This asserts the route stays gone rather than
    being reintroduced as a faster path around `users.visibility`.
    """

    def test_no_proximity_route_is_registered(self):
        from core.asgi import application

        router = application.application_mapping['websocket']
        patterns = []
        while hasattr(router, 'inner'):
            router = router.inner
        for route in router.routes:
            patterns.append(str(route.pattern))

        self.assertFalse(
            [p for p in patterns if 'proximity' in p],
            f'a proximity websocket is routed again: {patterns}',
        )
