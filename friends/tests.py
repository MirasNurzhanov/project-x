"""Blocking behavior: mutual invisibility, request rejection, unblock restore.

Run with:  python manage.py test friends --settings=core.test_settings
"""
from django.utils import timezone
from datetime import timedelta

from rest_framework import status
from rest_framework.test import APITestCase

from stories.models import Story
from users.models import User
from users.testing import check_in
from .models import BlockedUser, FriendRequest, HiRequest, Message

# ~100m apart — inside every visibility radius.
LAT_A, LON_A = 43.238949, 76.889709
LAT_B, LON_B = 43.239849, 76.889709


def make_user(name, lat, lon):
    user = User.objects.create_user(
        email=f'{name}@test.dev', username=name, password='pw',
    )
    user.latitude = lat
    user.longitude = lon
    user.save(update_fields=['latitude', 'longitude'])
    # On the map by default. Users are checked *out* on creation now (see
    # users.visibility), so a fixture for a test about anything else has to
    # opt in or it measures the check-in gate instead of its own subject.
    # proximity/test_check_in.py owns the assertions about the default.
    return check_in(user)


class BlockTestCase(APITestCase):
    def setUp(self):
        self.alice = make_user('alice', LAT_A, LON_A)
        self.bob = make_user('bob', LAT_B, LON_B)

    # ── helpers ──────────────────────────────────────────────────────────────
    def as_user(self, user):
        self.client.force_authenticate(user=user)

    def block(self, blocker, blocked):
        self.as_user(blocker)
        return self.client.post(f'/api/friends/{blocked.pk}/block/')

    def unblock(self, blocker, blocked):
        self.as_user(blocker)
        return self.client.delete(f'/api/friends/{blocked.pk}/unblock/')

    def nearby_ids(self, user):
        self.as_user(user)
        res = self.client.get('/api/proximity/nearby/')
        assert res.status_code == status.HTTP_200_OK, res.data
        return {u['id'] for u in res.data}

    def connect(self, a, b):
        HiRequest.objects.create(sender=a, receiver=b, status=HiRequest.Status.ACCEPTED)

    def befriend(self, a, b):
        self.connect(a, b)
        FriendRequest.objects.create(sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED)


class BlockEndpointTests(BlockTestCase):
    def test_block_list_unblock_roundtrip(self):
        self.assertEqual(self.block(self.alice, self.bob).status_code, 204)

        res = self.client.get('/api/friends/blocked/')
        self.assertEqual([u['id'] for u in res.data], [self.bob.pk])

        self.assertEqual(self.unblock(self.alice, self.bob).status_code, 204)
        res = self.client.get('/api/friends/blocked/')
        self.assertEqual(res.data, [])

    def test_block_deletes_pending_requests_both_directions(self):
        HiRequest.objects.create(sender=self.bob, receiver=self.alice)
        FriendRequest.objects.create(sender=self.alice, receiver=self.bob)

        self.block(self.alice, self.bob)

        self.assertFalse(HiRequest.objects.filter(sender=self.bob, receiver=self.alice).exists())
        self.assertFalse(FriendRequest.objects.filter(sender=self.alice, receiver=self.bob).exists())


class NearbyVisibilityTests(BlockTestCase):
    def test_blocked_user_excluded_from_nearby_both_directions(self):
        self.assertIn(self.bob.pk, self.nearby_ids(self.alice))
        self.assertIn(self.alice.pk, self.nearby_ids(self.bob))

        self.block(self.alice, self.bob)

        self.assertNotIn(self.bob.pk, self.nearby_ids(self.alice))
        # Mutual: bob cannot see alice either.
        self.assertNotIn(self.alice.pk, self.nearby_ids(self.bob))

    def test_unblock_restores_visibility(self):
        self.block(self.alice, self.bob)
        self.unblock(self.alice, self.bob)

        self.assertIn(self.bob.pk, self.nearby_ids(self.alice))
        self.assertIn(self.alice.pk, self.nearby_ids(self.bob))

    def test_blocked_friend_hidden_even_without_distance_limit(self):
        self.befriend(self.alice, self.bob)
        self.block(self.alice, self.bob)

        self.assertNotIn(self.bob.pk, self.nearby_ids(self.alice))
        self.assertNotIn(self.alice.pk, self.nearby_ids(self.bob))


class RequestRejectionTests(BlockTestCase):
    def test_hi_rejected_in_both_directions(self):
        self.block(self.alice, self.bob)

        self.as_user(self.bob)
        res = self.client.post('/api/friends/hi/', {'receiver_id': self.alice.pk})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        self.as_user(self.alice)
        res = self.client.post('/api/friends/hi/', {'receiver_id': self.bob.pk})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_friend_request_rejected_in_both_directions(self):
        self.connect(self.alice, self.bob)
        self.block(self.alice, self.bob)

        self.as_user(self.bob)
        res = self.client.post(f'/api/friends/request/{self.alice.pk}/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        self.as_user(self.alice)
        res = self.client.post(f'/api/friends/request/{self.bob.pk}/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_pending_hi_hidden_and_unacceptable_after_block(self):
        # Simulate a pending request that survives a block (e.g. race).
        self.block(self.alice, self.bob)
        hi = HiRequest.objects.create(sender=self.bob, receiver=self.alice)

        self.as_user(self.alice)
        res = self.client.get('/api/friends/requests/')
        self.assertEqual(res.data, [])

        res = self.client.post(f'/api/friends/hi/{hi.pk}/accept/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_pending_friend_request_hidden_and_unacceptable_after_block(self):
        self.block(self.alice, self.bob)
        fr = FriendRequest.objects.create(sender=self.bob, receiver=self.alice)

        self.as_user(self.alice)
        res = self.client.get('/api/friends/requests/incoming/')
        self.assertEqual(res.data, [])

        res = self.client.post(f'/api/friends/request/{fr.pk}/accept/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)


class FriendsListTests(BlockTestCase):
    def test_friends_list_mutually_invisible_and_no_blocked_by_leak(self):
        self.befriend(self.alice, self.bob)
        self.block(self.alice, self.bob)

        self.as_user(self.alice)
        data = self.client.get('/api/friends/list/').data
        self.assertEqual(data['friends'], [])
        self.assertEqual(data['connections'], [])

        # The blocked side sees the same absence — and no flag says why.
        self.as_user(self.bob)
        data = self.client.get('/api/friends/list/').data
        self.assertEqual(data['friends'], [])
        self.assertEqual(data['connections'], [])
        for group in data.values():
            for entry in group:
                self.assertNotIn('blocked_by', entry)

    def test_unblock_restores_friendship(self):
        self.befriend(self.alice, self.bob)
        self.block(self.alice, self.bob)
        self.unblock(self.alice, self.bob)

        self.as_user(self.alice)
        data = self.client.get('/api/friends/list/').data
        self.assertEqual([u['id'] for u in data['friends']], [self.bob.pk])

        self.as_user(self.bob)
        data = self.client.get('/api/friends/list/').data
        self.assertEqual([u['id'] for u in data['friends']], [self.alice.pk])


class MessagingTests(BlockTestCase):
    def test_messages_blocked_both_directions(self):
        self.connect(self.alice, self.bob)
        Message.objects.create(sender=self.bob, receiver=self.alice, content='hey')
        self.block(self.alice, self.bob)

        self.as_user(self.bob)
        res = self.client.post(f'/api/friends/messages/{self.alice.pk}/', {'content': 'hi'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = self.client.get(f'/api/friends/messages/{self.alice.pk}/')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        self.as_user(self.alice)
        res = self.client.post(f'/api/friends/messages/{self.bob.pk}/', {'content': 'hi'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_unread_counts_exclude_blocked_sender(self):
        self.connect(self.alice, self.bob)
        Message.objects.create(sender=self.bob, receiver=self.alice, content='hey')

        self.as_user(self.alice)
        self.assertEqual(self.client.get('/api/friends/messages/unread/').data['unread_count'], 1)

        self.block(self.alice, self.bob)
        self.assertEqual(self.client.get('/api/friends/messages/unread/').data['unread_count'], 0)
        self.assertEqual(self.client.get('/api/friends/messages/unread/breakdown/').data, [])


class StoryVisibilityTests(BlockTestCase):
    def make_story(self, user, lat, lon):
        return Story.objects.create(
            user=user, content='story', latitude=lat, longitude=lon,
            expires_at=timezone.now() + timedelta(hours=24),
        )

    def test_blocked_users_stories_hidden_both_directions(self):
        story_b = self.make_story(self.bob, LAT_B, LON_B)
        story_a = self.make_story(self.alice, LAT_A, LON_A)
        self.block(self.alice, self.bob)

        self.as_user(self.alice)
        ids = {s['id'] for s in self.client.get('/api/stories/nearby/').data}
        self.assertNotIn(story_b.pk, ids)
        self.assertIn(story_a.pk, ids)

        self.as_user(self.bob)
        ids = {s['id'] for s in self.client.get('/api/stories/nearby/').data}
        self.assertNotIn(story_a.pk, ids)
        self.assertIn(story_b.pk, ids)

    def test_cannot_save_blocked_users_story(self):
        story_b = self.make_story(self.bob, LAT_B, LON_B)
        self.block(self.alice, self.bob)

        self.as_user(self.alice)
        res = self.client.post(f'/api/stories/{story_b.pk}/save/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_saved_list_hides_stories_from_blocked_author(self):
        from stories.models import SavedStory
        story_b = self.make_story(self.bob, LAT_B, LON_B)
        SavedStory.objects.create(user=self.alice, story=story_b)

        self.block(self.alice, self.bob)
        self.as_user(self.alice)
        res = self.client.get('/api/stories/saved/')
        self.assertEqual(res.data['results'], [])
