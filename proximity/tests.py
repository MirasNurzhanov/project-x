from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import FriendRequest, HiRequest
from interests.models import Interest
from notifications.models import Notification
from notifications.utils import notify_zone_music_entry
from proximity.views import _trigger_zone_music_alerts
from users.models import User

#: Stands in for "this user has a zone track" in tests that only care about
#: visibility gating, not about what the track actually is.
TEST_TRACK_URL = 'https://example.com/test-track.mp3'


class NearbyZoneAudioTests(TestCase):
    """Zone music is friends-and-connections-only: /api/proximity/nearby/
    must not expose another user's zone audio to a stranger, however the
    owner's Zone audio setting is configured."""

    def setUp(self):
        self.client = APIClient()
        self.me = self._make_user('me', latitude='10.0', longitude='10.0')
        self.other = self._make_user(
            'other',
            latitude='10.001',
            longitude='10.001',
            zone_apple_preview_url=TEST_TRACK_URL,
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

    def test_connected_via_hi_gets_zone_audio(self):
        HiRequest.objects.create(
            sender=self.me, receiver=self.other, status=HiRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'connected')
        self.assertEqual(entry['zone_audio_url'], TEST_TRACK_URL)

    def test_friend_gets_zone_audio(self):
        FriendRequest.objects.create(
            sender=self.other, receiver=self.me, status=FriendRequest.Status.ACCEPTED
        )
        entry = self._nearby_by_id()[self.other.pk]
        self.assertEqual(entry['relationship'], 'friend')
        self.assertEqual(entry['zone_audio_url'], TEST_TRACK_URL)


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

    It used to gate the music too — the music-zone circles, a friend's ability
    to play the track, and these alerts — so removing the circle from the map
    silently muted a song its owner had never touched. The switch is labelled
    "Zone on the map"; these pin the behaviour to the label.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', email='owner@example.com', password='testpass-123',
            latitude='10.0', longitude='10.0',
            zone_apple_preview_url=TEST_TRACK_URL,
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

    def test_a_friend_outside_the_music_zone_is_still_not_alerted(self):
        """Decoupling the switch must not widen who counts as being inside the
        music zone — that is still its radius, and only its radius."""
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


class MusicZoneSwitchTests(TestCase):
    """`music_zone_active` is the one switch the music zone has.

    The counterpart to ZoneSwitchScopeTests above: that one pins that removing
    the map circle leaves the music alone, this one pins that removing the music
    zone leaves the map circle alone. Off means gone — no track sent, no alert,
    and no radius for anyone to draw a circle from, which is how the zone comes
    off every viewer's map.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username='mute-owner', email='mute-owner@example.com', password='testpass-123',
            latitude='10.0', longitude='10.0',
            zone_apple_preview_url=TEST_TRACK_URL,
            zone_active=True,           # circle very much on the map
            zone_radius=500,
            music_zone_alerts=True,
            music_zone_active=False,    # …and no music zone at all
        )
        self.visitor = User.objects.create_user(
            username='mute-visitor', email='mute-visitor@example.com', password='testpass-123',
            latitude='10.001', longitude='10.001',
        )
        FriendRequest.objects.create(
            sender=self.visitor, receiver=self.owner,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.visitor)

    def _nearby_entry(self):
        return next(
            row for row in self.client.get('/api/proximity/nearby/').json()
            if row['id'] == self.owner.pk
        )

    def test_defaults_to_on(self):
        """Every zone that existed before this field keeps broadcasting."""
        fresh = User.objects.create_user(
            username='fresh', email='fresh@example.com', password='testpass-123',
        )
        self.assertTrue(fresh.music_zone_active)

    def test_a_friend_is_not_sent_the_track(self):
        entry = self._nearby_entry()
        self.assertNotIn('zone_audio_url', entry)
        self.assertNotIn('zone_audio', entry)

    def test_the_radius_goes_with_it(self):
        """Withheld, not nulled — and this absence is the whole feature. The
        map draws a friend's music zone from this radius, so a viewer who is
        sent one has a circle to draw and a viewer who is not has nothing."""
        entry = self._nearby_entry()
        self.assertNotIn('music_zone_radius', entry)

    def test_the_map_zone_is_untouched(self):
        """Removing the music zone must not take the map circle with it."""
        entry = self._nearby_entry()
        self.assertTrue(entry['zone_active'])
        self.assertEqual(entry['zone_radius'], 500)

    def test_no_alert_in_either_direction(self):
        notify_zone_music_entry(self.visitor, self.owner, 120)
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.visitor).exists())

    def test_the_sweep_skips_an_owner_with_no_music_zone(self):
        _trigger_zone_music_alerts(self.visitor)
        self.assertFalse(Notification.objects.filter(recipient=self.owner).exists())

    def test_switching_it_back_on_restores_all_of_it(self):
        """Off parks the zone rather than dismantling it — the track, the
        radius and the alerts all come back with one switch."""
        self.owner.music_zone_active = True
        self.owner.save()
        entry = self._nearby_entry()
        self.assertEqual(entry['zone_audio_url'], TEST_TRACK_URL)
        # The music zone's own radius, never the map zone's 500.
        self.assertEqual(entry['music_zone_radius'], 200)
        notify_zone_music_entry(self.visitor, self.owner, 120)
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())

    def test_the_radius_is_its_own_in_region_mode_too(self):
        """A music zone is always a circle. It used to inherit a region border
        when it was not 'separate', which meant alerting on everyone in town —
        so the sweep skipped region-mode owners entirely. There is nothing to
        inherit now, so they are swept like anyone else."""
        from regions.models import Region

        self.owner.music_zone_active = True
        self.owner.zone_type = User.ZoneType.CITY
        self.owner.zone_region = Region.objects.create(
            name='Testville', display_name='Testville, Testland',
            min_lat=9.0, min_lon=9.0, max_lat=11.0, max_lon=11.0,
            centroid_lat=10.0, centroid_lon=10.0,
        )
        self.owner.save()
        self.assertEqual(self.owner.effective_zone_mode, 'region')
        entry = self._nearby_entry()
        self.assertEqual(entry['music_zone_radius'], 200)
        _trigger_zone_music_alerts(self.visitor)   # ~157m away, inside 200m
        self.assertTrue(Notification.objects.filter(recipient=self.owner).exists())

    def test_the_owner_still_sees_their_own_track(self):
        """Switching the zone off hides you from other people, not your song
        from you — the Zone Music screen has to keep showing what is loaded."""
        client = APIClient()
        client.force_authenticate(self.owner)
        data = client.get('/api/users/zone/').json()
        self.assertEqual(data['zone_audio_url'], TEST_TRACK_URL)
        self.assertFalse(data['music_zone_active'])
