from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import FriendRequest, HiRequest
from interests.models import Interest
from users.models import User
from users.presets import ZONE_PRESETS


class NearbyZoneAudioTests(TestCase):
    """Zone music is friends-only: /api/proximity/nearby/ must not expose
    another user's zone audio unless the requester is an accepted friend."""

    def setUp(self):
        self.client = APIClient()
        self.me = self._make_user('me', latitude='10.0', longitude='10.0')
        self.other = self._make_user(
            'other',
            latitude='10.001',
            longitude='10.001',
            zone_preset=ZONE_PRESETS[0]['id'],
            zone_active=True,
        )
        self.client.force_authenticate(self.me)

    @staticmethod
    def _make_user(name, **extra):
        return User.objects.create_user(
            username=name, email=f'{name}@example.com', password='testpass-123', **extra
        )

    def _nearby_by_id(self):
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        return {entry['id']: entry for entry in response.json()}

    def test_stranger_gets_no_zone_audio(self):
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'stranger')
        self.assertIsNone(entry['zone_audio'])
        self.assertIsNone(entry['zone_audio_url'])
        # Zone presence stays visible so the map can still draw the circle
        self.assertTrue(entry['zone_active'])

    def test_pending_friend_request_gets_no_zone_audio(self):
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other, status=FriendRequest.Status.PENDING
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertIsNone(entry['zone_audio'])
        self.assertIsNone(entry['zone_audio_url'])

    def test_connected_via_hi_gets_no_zone_audio(self):
        HiRequest.objects.create(
            sender=self.me, receiver=self.other, status=HiRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'connected')
        self.assertIsNone(entry['zone_audio'])
        self.assertIsNone(entry['zone_audio_url'])

    def test_friend_gets_zone_audio(self):
        FriendRequest.objects.create(
            sender=self.other, receiver=self.me, status=FriendRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'friend')
        self.assertEqual(entry['zone_audio_url'], ZONE_PRESETS[0]['url'])


class NearbySharedInterestsTests(TestCase):
    """/api/proximity/nearby/ powers the map's interest matching: strangers are
    filtered to interest matches server-side and each entry carries the
    shared_interests names the map renders; friends and connections must stay
    visible even without any shared interest."""

    def setUp(self):
        self.client = APIClient()
        self.music = Interest.objects.create(name='Test Music')
        self.hiking = Interest.objects.create(name='Test Hiking')
        self.chess = Interest.objects.create(name='Test Chess')
        self.me = self._make_user('me')
        self.me.interests.set([self.music, self.hiking])
        self.client.force_authenticate(self.me)

    @staticmethod
    def _make_user(name):
        return User.objects.create_user(
            username=name,
            email=f'{name}@example.com',
            password='testpass-123',
            latitude='10.0',
            longitude='10.0',
        )

    def _nearby_by_id(self):
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        return {entry['id']: entry for entry in response.json()}

    def test_matching_stranger_listed_with_shared_interest_names(self):
        match = self._make_user('match')
        match.interests.set([self.hiking, self.chess])
        entry = self._nearby_by_id()[match.pk]
        self.assertEqual(entry['relationship'], 'stranger')
        self.assertEqual(entry['shared_interests'], ['Test Hiking'])

    def test_stranger_without_shared_interest_excluded(self):
        loner = self._make_user('loner')
        loner.interests.set([self.chess])
        self.assertNotIn(loner.pk, self._nearby_by_id())

    def test_friend_without_shared_interest_still_listed(self):
        friend = self._make_user('friend')
        friend.interests.set([self.chess])
        FriendRequest.objects.create(
            sender=friend, receiver=self.me, status=FriendRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[friend.pk]
        self.assertEqual(entry['relationship'], 'friend')
        self.assertEqual(entry['shared_interests'], [])

    def test_connection_without_shared_interest_still_listed(self):
        buddy = self._make_user('buddy')
        HiRequest.objects.create(
            sender=self.me, receiver=buddy, status=HiRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[buddy.pk]
        self.assertEqual(entry['relationship'], 'connected')
        self.assertEqual(entry['shared_interests'], [])

    def test_requester_without_interests_sees_everyone_unmatched(self):
        self.me.interests.clear()
        stranger = self._make_user('stranger')
        stranger.interests.set([self.chess])
        entry = self._nearby_by_id()[stranger.pk]
        self.assertEqual(entry['shared_interests'], [])
