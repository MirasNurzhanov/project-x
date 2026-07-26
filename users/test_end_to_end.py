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


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


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
        for user in (self.me, self.pest):
            user.visibility_stories = User.Audience.EVERYONE
            user.save(update_fields=['visibility_stories'])
            Story.objects.create(
                user=user, content='hi', latitude='10.0005', longitude='10.0005',
                expires_at=timezone.now() + timezone.timedelta(hours=5),
            )
        self.block()
        mine = self.my_client.get('/api/stories/nearby/').json()
        theirs = self.their_client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in mine if s['author']['id'] == self.pest.pk], [])
        self.assertEqual([s for s in theirs if s['author']['id'] == self.me.pk], [])

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
        self.pest = make_user('pest', visibility_stories=User.Audience.EVERYONE)
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


class ConsumerParityTests(TestCase):
    """The WebSocket returns its own hand-rolled payload rather than going
    through the serializer, so it is the most likely place for a rule to be
    missing. Assert it agrees with the REST endpoint."""

    def setUp(self):
        self.interest = Interest.objects.create(name='Test Sailing')
        self.me = make_user('me', latitude='10.0000', longitude='10.0000')
        self.other = make_user('other', latitude='10.0010', longitude='10.0010')
        for user in (self.me, self.other):
            user.interests.set([self.interest])

    def consumer_ids(self, lat='10.0000', lon='10.0000'):
        from proximity.consumers import LocationConsumer

        consumer = LocationConsumer()
        consumer.user = self.me
        return {entry['id'] for entry in consumer._update_and_find_nearby.__wrapped__(
            consumer, float(lat), float(lon)
        )}

    def test_paused_user_hidden_from_the_socket(self):
        self.assertIn(self.other.pk, self.consumer_ids())
        self.other.location_sharing = User.LocationSharing.PAUSED
        self.other.save(update_fields=['location_sharing'])
        self.assertNotIn(self.other.pk, self.consumer_ids())

    def test_blocked_user_hidden_from_the_socket(self):
        BlockedUser.objects.create(blocker=self.other, blocked=self.me)
        self.assertNotIn(self.other.pk, self.consumer_ids())

    def test_interest_opt_out_hidden_from_the_socket(self):
        self.other.discoverable_by_interest = False
        self.other.save(update_fields=['discoverable_by_interest'])
        self.assertNotIn(self.other.pk, self.consumer_ids())

    def test_socket_fuzzes_an_approximate_reporters_own_position(self):
        self.me.location_sharing = User.LocationSharing.APPROXIMATE
        self.me.save(update_fields=['location_sharing'])
        self.consumer_ids('52.37021900', '4.89516700')
        self.me.refresh_from_db()
        self.assertNotEqual(str(self.me.latitude), '52.37021900')
