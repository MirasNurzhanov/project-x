from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import FriendRequest, HiRequest
from interests.models import Interest
from notifications.models import Notification
from notifications.utils import notify_zone_music_entry
from proximity.views import _trigger_zone_music_alerts
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

    # The zone fields are now omitted entirely rather than sent as null, so a
    # modified client has nothing to notice. Asserting absence is the stronger
    # check: a null would still tell the reader the field exists.
    def test_stranger_gets_no_zone_audio(self):
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'stranger')
        self.assertNotIn('zone_audio', entry)
        self.assertNotIn('zone_audio_url', entry)
        # Zone presence stays visible so the map can still draw the circle
        self.assertTrue(entry['zone_active'])

    def test_pending_friend_request_gets_no_zone_audio(self):
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other, status=FriendRequest.Status.PENDING
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertNotIn('zone_audio', entry)
        self.assertNotIn('zone_audio_url', entry)

    def test_connected_via_hi_gets_no_zone_audio(self):
        HiRequest.objects.create(
            sender=self.me, receiver=self.other, status=HiRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'connected')
        self.assertNotIn('zone_audio', entry)
        self.assertNotIn('zone_audio_url', entry)

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


class ZoneSwitchScopeTests(TestCase):
    """`zone_active` turns off the zone *drawn on the map*, and nothing else.

    It used to gate the music too — the earshot circles, a friend's ability to
    play the track, and these alerts — so removing the circle from the map
    silently muted a song its owner had never touched. The switch is labelled
    "Zone on the map"; these pin the behaviour to the label.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', email='owner@example.com', password='testpass-123',
            latitude='10.0', longitude='10.0',
            zone_preset=ZONE_PRESETS[0]['id'],
            zone_active=False,          # circle removed from the map
            zone_radius=500,
            music_zone_alerts=True,
        )
        self.visitor = User.objects.create_user(
            username='visitor', email='visitor@example.com', password='testpass-123',
            latitude='10.001', longitude='10.001',
        )
        FriendRequest.objects.create(
            sender=self.visitor, receiver=self.owner,
            status=FriendRequest.Status.ACCEPTED,
        )

    def alerts_for(self, user):
        return Notification.objects.filter(recipient=user)

    def test_music_alerts_still_fire_with_the_map_zone_off(self):
        notify_zone_music_entry(self.visitor, self.owner, 120)
        self.assertTrue(self.alerts_for(self.owner).exists())

    def test_no_track_means_no_alert(self):
        """The track is the switch that was always meant to do this job."""
        self.owner.zone_preset = ''
        self.owner.zone_audio = None
        self.owner.zone_apple_preview_url = ''
        self.owner.save()
        notify_zone_music_entry(self.visitor, self.owner, 120)
        self.assertFalse(self.alerts_for(self.owner).exists())

    def test_the_sweep_still_considers_an_owner_with_the_map_zone_off(self):
        """The queryset used to filter these owners out before anything else
        got a chance to look at them."""
        _trigger_zone_music_alerts(self.visitor)
        self.assertTrue(self.alerts_for(self.owner).exists())

    def test_a_friend_out_of_earshot_is_still_not_alerted(self):
        """Decoupling the switch must not widen who counts as being in
        earshot — that is still the radius, and only the radius."""
        self.visitor.latitude, self.visitor.longitude = 20.0, 20.0
        self.visitor.save()
        _trigger_zone_music_alerts(self.visitor)
        self.assertFalse(self.alerts_for(self.owner).exists())

    def test_muting_music_zone_alerts_still_silences_them(self):
        self.owner.music_zone_alerts = False
        self.owner.save()
        notify_zone_music_entry(self.visitor, self.owner, 120)
        self.assertFalse(self.alerts_for(self.owner).exists())

    def test_a_friend_is_still_sent_the_track_with_the_map_zone_off(self):
        """The client used to drop this URL when the zone was off. The server
        always sent it; nothing here should have to change for the map to stop
        throwing it away, but it is worth pinning that it really does arrive."""
        client = APIClient()
        client.force_authenticate(self.visitor)
        entry = next(
            row for row in client.get('/api/proximity/nearby/').json()
            if row['id'] == self.owner.pk
        )
        self.assertFalse(entry['zone_active'])
        self.assertTrue(entry.get('zone_audio_url'))
