"""Swift music is only ever streamed from Approx's own storage.

The claim this protects, made to App Review under guideline 5.2.3: the app does
not connect to a music provider to play Swift music. `sync` imports provider
URLs as-is, so the guarantee has to live where tracks are served — see
swifts/music/hosting.py. Test settings put Approx's public media on
`https://test.example.com`.
"""
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SwiftTrack
from .music.hosting import is_self_hosted, media_hosts
from .test_creator_features import publish_payload
from .testing import SwiftTestCase, make_user

OURS = 'https://test.example.com/music/1.mp3'
PROVIDER = 'https://cdn.epidemicsound.com/a/1001.mp3'


def make_track(audio_url=OURS, **extra):
    fields = {
        'title': 'Night Drive', 'artist': 'Approx Sounds', 'genre': 'Lo-fi',
        'audio_url': audio_url, 'duration_ms': 120_000,
        'source': SwiftTrack.Source.LICENSED, 'licence': 'Test licence',
        'is_active': True,
    }
    fields.update(extra)
    return SwiftTrack.objects.create(**fields)


class HostRuleTests(SwiftTestCase):
    def test_public_media_host_counts_as_ours(self):
        self.assertIn('test.example.com', media_hosts())
        self.assertTrue(is_self_hosted(OURS))

    def test_provider_cdn_plain_http_and_lookalikes_do_not(self):
        for url in (
            PROVIDER,
            'http://test.example.com/music/1.mp3',
            'https://test.example.com.evil.test/music/1.mp3',
            '',
        ):
            self.assertFalse(is_self_hosted(url), url)

    @override_settings(SWIFT_MUSIC_MEDIA_HOSTS=('media.approx.test',))
    def test_extra_hosts_come_from_settings(self):
        self.assertTrue(is_self_hosted('https://media.approx.test/music/1.mp3'))

    def test_servable_excludes_provider_hosted_and_lookalike_rows(self):
        mine = make_track()
        make_track(audio_url=PROVIDER, title='Provider')
        make_track(audio_url='https://test.example.com.evil.test/x.mp3', title='Lookalike')
        make_track(is_active=False, title='Unpublished')

        self.assertEqual(list(SwiftTrack.objects.servable()), [mine])


class PickerTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(make_user('creator'))

    def test_picker_lists_only_self_hosted_tracks(self):
        make_track(title='Ours')
        make_track(audio_url=PROVIDER, title='Theirs')

        body = self.client.get(reverse('swift-tracks')).json()
        self.assertEqual([t['title'] for t in body['results']], ['Ours'])

    def test_provider_artwork_is_not_handed_to_the_app(self):
        make_track(artwork_url='https://cdn.epidemicsound.com/a/1001.jpg')

        body = self.client.get(reverse('swift-tracks')).json()
        self.assertEqual(body['results'][0]['artwork_url'], '')

    def test_a_provider_hosted_track_cannot_be_attached(self):
        track = make_track(audio_url=PROVIDER)
        response = self.client.post(
            reverse('swift-create'), publish_payload(track=track.pk))
        self.assertEqual(response.status_code, 400)
        self.assertIn('track', response.data)


class PublishedSwiftTests(SwiftTestCase):
    def test_a_swift_whose_track_moved_off_our_storage_plays_no_music_bed(self):
        author = make_user('author')
        client = APIClient()
        client.force_authenticate(author)
        track = make_track(artwork_url='https://test.example.com/music/1.jpg')
        created = client.post(reverse('swift-create'), publish_payload(
            track=track.pk, uses_original_audio=False,
        ))
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data['track']['audio_url'], OURS)

        SwiftTrack.objects.filter(pk=track.pk).update(audio_url=PROVIDER)
        detail = client.get(reverse('swift-detail', args=[created.data['id']])).json()

        self.assertEqual(detail['track']['audio_url'], '')
        # The credit survives — only the stream is withheld.
        self.assertEqual(detail['track']['title'], 'Night Drive')
