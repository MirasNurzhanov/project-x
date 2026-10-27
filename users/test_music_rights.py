"""Music in Approx is either an Apple Music preview or the sound a Swift was
recorded with (App Review guideline 5.2.3).

Zones no longer take uploads — an uploaded file is somebody else's music as
often as not, and there is no way to show App Review the rights to it — and a
new Swift cannot carry a music track until Approx holds a licence. Apple Music
previews link back to the song on Apple Music.
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from proximity.serializers import _zone_track_url
from swifts.serializers import SwiftCreateSerializer
from users.models import User

ZONE_URL = '/api/users/zone/'


class ZoneUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='testpass-123')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_an_upload_is_refused(self):
        upload = SimpleUploadedFile('song.mp3', b'ID3' + b'\0' * 64, content_type='audio/mpeg')
        response = self.client.patch(ZONE_URL, {'zone_audio': upload}, format='multipart')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()['code'], ['zone_upload_disabled'])
        self.user.refresh_from_db()
        self.assertFalse(self.user.zone_audio)

    def test_clearing_an_old_upload_still_works(self):
        response = self.client.patch(ZONE_URL, {'zone_audio': None}, format='json')
        self.assertEqual(response.status_code, 200, response.content)

    def test_an_old_upload_is_played_to_nobody(self):
        User.objects.filter(pk=self.user.pk).update(zone_audio='zones/old.mp3')
        self.user.refresh_from_db()
        self.assertFalse(self.user.has_zone_track)
        self.assertIsNone(_zone_track_url(self.user))
        self.assertIsNone(self.client.get(ZONE_URL).json()['zone_audio_url'])


class AppleMusicLinkTests(TestCase):
    def test_the_zone_links_its_song_on_apple_music(self):
        user = User.objects.create_user(
            username='owner', email='owner@example.com', password='testpass-123',
            zone_apple_id='1488408568', zone_apple_preview_url='https://example.com/p.m4a',
        )
        client = APIClient()
        client.force_authenticate(user)
        zone = client.get(ZONE_URL).json()
        self.assertEqual(zone['zone_apple_url'], 'https://music.apple.com/us/song/1488408568')

    def test_no_song_no_link(self):
        user = User.objects.create_user(username='quiet', email='quiet@example.com', password='testpass-123')
        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.get(ZONE_URL).json()['zone_apple_url'], '')


class SwiftMusicTests(TestCase):
    @override_settings(SWIFT_MUSIC_ENABLED=False)
    def test_a_new_swift_cannot_carry_a_track_while_music_is_off(self):
        serializer = SwiftCreateSerializer()
        with self.assertRaisesMessage(Exception, 'recorded with'):
            serializer.validate_track(object())
        self.assertIsNone(serializer.validate_track(None))
