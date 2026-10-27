"""What the location report says is playing where the mover stands.

The app plays a friend's zone track once when its owner's music zone is
entered, including while the app is closed, and it learns about that from the
response to its own location report. So the response has to name the track
exactly when the listener may hear it, and nothing otherwise.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from friends.models import FriendRequest
from users.models import User

LOCATION_URL = '/api/proximity/location/'


def make_user(name, latitude, **extra):
    fields = dict(
        latitude=latitude, longitude=10, ghost_mode=False,
        location_updated_at=timezone.now(), location_accuracy_m=5,
        music_zone_active=True, music_zone_radius=200, music_zone_alerts=True,
    )
    fields.update(extra)
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **fields,
    )


class ZoneMusicHereTests(TestCase):
    def setUp(self):
        self.owner = make_user(
            'owner', 10,
            zone_apple_preview_url='https://example.com/preview.m4a',
            zone_apple_title='Blinding Lights', zone_apple_artist='The Weeknd',
            zone_apple_artwork='https://example.com/art.jpg',
        )
        self.visitor = make_user('visitor', 9.99)
        FriendRequest.objects.create(
            sender=self.visitor, receiver=self.owner, status=FriendRequest.Status.ACCEPTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.visitor)

    def report(self, latitude):
        response = self.client.patch(
            LOCATION_URL, {'latitude': latitude, 'longitude': 10, 'accuracy': 5}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['zone_music']

    def test_names_the_track_inside_a_friends_zone(self):
        music = self.report(10.0005)  # ~55 m
        self.assertEqual(music['owner_id'], self.owner.pk)
        self.assertEqual(music['url'], 'https://example.com/preview.m4a')
        self.assertEqual((music['title'], music['artist']), ('Blinding Lights', 'The Weeknd'))
        self.assertEqual((music['start_ms'], music['end_ms']), (0, None))

    def test_says_nothing_outside_it(self):
        self.assertIsNone(self.report(10.01))  # ~1.1 km

    def test_keeps_naming_it_after_the_alert_has_cooled_down(self):
        self.report(10.0005)
        self.assertIsNotNone(self.report(10.0004))

    def test_says_nothing_when_the_music_zone_is_off(self):
        User.objects.filter(pk=self.owner.pk).update(music_zone_active=False)
        self.assertIsNone(self.report(10.0005))

    def test_says_nothing_for_a_stranger(self):
        FriendRequest.objects.all().delete()
        self.assertIsNone(self.report(10.0005))

    def test_says_nothing_to_a_ghost(self):
        self.visitor.ghost_mode = True
        self.visitor.save(update_fields=['ghost_mode'])
        self.assertIsNone(self.report(10.0005))

    def test_sends_the_trimmed_slice(self):
        User.objects.filter(pk=self.owner.pk).update(zone_track_start_ms=5000, zone_track_end_ms=20000)
        music = self.report(10.0005)
        self.assertEqual((music['start_ms'], music['end_ms']), (5000, 20000))
