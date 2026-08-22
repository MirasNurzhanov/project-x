"""The media pipeline after the R2 migration.

There is no transformation grammar to test any more — `playback_url` and
`poster_url` are a straight choice between the uploaded file and a legacy
fallback column (see `swifts.media`). What is still worth testing here: that
choice, the multipart create path that replaced the signed-upload endpoint,
and the track catalogue (browsing only — attaching a track to a Swift is
gone with the compositing that used to mix it in).
"""
from unittest.mock import patch

from django.urls import reverse
from rest_framework.test import APIClient

from . import media
from .models import SwiftTrack, SwiftVideo
from .testing import SwiftTestCase, fake_image, fake_video, make_swift, make_user


def make_track(**extra):
    fields = {
        'title': 'Night Drive', 'artist': 'Aura Sounds', 'genre': 'Lo-fi',
        'audio_url': 'https://cdn.example.com/a/night.mp3',
        'duration_ms': 120_000,
        'source': SwiftTrack.Source.OWNED,
        'licence': 'Commissioned 2026-01, contract A-441',
        'is_active': True,
    }
    fields.update(extra)
    return SwiftTrack.objects.create(**fields)


class PlaybackUrlTests(SwiftTestCase):
    """The uploaded file wins; the legacy URL is the fallback for a row that
    predates it."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')

    def test_an_uploaded_video_resolves_to_its_own_url(self):
        swift = make_swift(self.creator, video=fake_video())
        self.assertTrue(media.playback_url(swift))
        self.assertNotEqual(media.playback_url(swift), swift.video_url)

    def test_a_legacy_row_falls_back_to_video_url(self):
        """`make_swift` gives every row a `video_url` by default — this is the
        shape a row from before the migration actually has."""
        swift = make_swift(self.creator)
        self.assertEqual(media.playback_url(swift), swift.video_url)

    def test_a_captured_thumbnail_resolves_to_its_own_url(self):
        swift = make_swift(self.creator, thumbnail=fake_image())
        self.assertTrue(media.poster_url(swift))
        self.assertNotEqual(media.poster_url(swift), swift.thumbnail_url)

    def test_a_legacy_row_falls_back_to_thumbnail_url(self):
        swift = make_swift(self.creator)
        self.assertEqual(media.poster_url(swift), swift.thumbnail_url)

    def test_a_row_with_neither_has_no_poster(self):
        swift = make_swift(self.creator, thumbnail_url='')
        self.assertEqual(media.poster_url(swift), '')


class CreateUploadTests(SwiftTestCase):
    """The multipart create path that replaced the signed-upload endpoint."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(make_user('creator'))

    def post(self, **extra):
        payload = {'video': fake_video(), 'duration_ms': 10_000}
        payload.update(extra)
        return self.client.post(reverse('swift-create'), payload, format='multipart')

    def test_a_video_file_is_required(self):
        response = self.client.post(
            reverse('swift-create'), {'duration_ms': 10_000}, format='multipart',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('video', response.json())

    def test_a_video_within_the_ceiling_is_accepted(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)

    def test_an_oversized_video_is_refused(self):
        with patch.object(media, 'MAX_UPLOAD_BYTES', 10):
            response = self.post(video=fake_video(content=b'more than ten bytes'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('video', response.json())

    def test_there_is_no_upload_signature_route_any_more(self):
        with self.assertRaises(Exception):
            reverse('swift-upload-signature')


class TrackCatalogueTests(SwiftTestCase):
    """Browsing the catalogue. Attaching a track to a Swift is gone with the
    server-side mixing that used to make it audible — see `swifts.media`."""

    def setUp(self):
        super().setUp()
        self.user = make_user('creator')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_only_published_tracks_are_listed(self):
        make_track(title='Published')
        make_track(title='Draft', is_active=False)

        body = self.client.get(reverse('swift-tracks')).json()
        titles = [t['title'] for t in body['results']]

        self.assertIn('Published', titles)
        self.assertNotIn('Draft', titles)

    def test_licence_and_source_are_never_sent_to_creators(self):
        """The operator's record of why a track is safe, not the creator's."""
        make_track()
        raw = self.client.get(reverse('swift-tracks')).content.decode()

        self.assertNotIn('licence', raw)
        self.assertNotIn('contract A-441', raw)
        self.assertNotIn('source', raw)

    def test_attribution_is_sent_because_a_licence_may_require_it(self):
        make_track(attribution='Night Drive by Aura Sounds (CC BY 4.0)')
        body = self.client.get(reverse('swift-tracks')).json()
        self.assertEqual(
            body['results'][0]['attribution'],
            'Night Drive by Aura Sounds (CC BY 4.0)')

    def test_search_matches_title_artist_and_genre(self):
        make_track(title='Night Drive', artist='Aura Sounds', genre='Lo-fi')
        make_track(title='Sunrise', artist='Other', genre='Ambient')

        for query, expected in (('night', 'Night Drive'), ('aura', 'Night Drive'),
                                ('ambient', 'Sunrise')):
            with self.subTest(query=query):
                body = self.client.get(f"{reverse('swift-tracks')}?q={query}").json()
                self.assertEqual([t['title'] for t in body['results']], [expected])

    def test_genre_filter(self):
        make_track(title='A', genre='Lo-fi')
        make_track(title='B', genre='Ambient')
        body = self.client.get(f"{reverse('swift-tracks')}?genre=ambient").json()
        self.assertEqual([t['title'] for t in body['results']], ['B'])

    def test_genres_endpoint_lists_only_published_genres(self):
        make_track(genre='Lo-fi')
        make_track(genre='Secret', is_active=False)

        body = self.client.get(reverse('swift-track-genres')).json()
        self.assertIn('Lo-fi', body['results'])
        self.assertNotIn('Secret', body['results'])

    def test_the_credit_survives_the_track_being_withdrawn(self):
        """A licence lapses; a Swift that predates the mixing removal — and so
        still carries a `track` FK — keeps its attribution. Exercised at the
        model level: nothing writes `track` through the API any more."""
        track = make_track()
        swift = make_swift(
            self.user, track=track,
            audio_title=track.title, audio_artist=track.artist,
        )
        track.delete()
        swift.refresh_from_db()

        self.assertIsNone(swift.track_id)
        self.assertEqual(swift.audio_title, 'Night Drive')

        body = self.client.get(reverse('swift-detail', args=[swift.pk])).json()
        self.assertEqual(body['track']['title'], 'Night Drive')
        self.assertIsNone(body['track']['id'])


class DurationAndTrimValidationTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(make_user('creator'))

    def post(self, **extra):
        payload = {'video': fake_video(), 'duration_ms': 10_000}
        payload.update(extra)
        return self.client.post(reverse('swift-create'), payload, format='multipart')

    def test_over_the_ceiling_is_refused(self):
        response = self.post(duration_ms=media.MAX_DURATION_MS + 1)
        self.assertEqual(response.status_code, 400)
        self.assertIn('duration_ms', response.json())

    def test_a_backwards_trim_is_refused(self):
        response = self.post(trim_start_ms=8000, trim_end_ms=2000)
        self.assertEqual(response.status_code, 400)
        self.assertIn('trim_end_ms', response.json())

    def test_a_trim_shorter_than_a_second_is_refused(self):
        response = self.post(trim_start_ms=1000, trim_end_ms=1500)
        self.assertEqual(response.status_code, 400)

    def test_a_trim_longer_than_the_ceiling_is_refused(self):
        response = self.post(trim_start_ms=0, trim_end_ms=media.MAX_DURATION_MS + 5000)
        self.assertEqual(response.status_code, 400)

    def test_a_sane_trim_is_accepted_and_stored(self):
        response = self.post(trim_start_ms=1000, trim_end_ms=9000)
        self.assertEqual(response.status_code, 201, response.content)

        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.trim_start_ms, 1000)
        self.assertEqual(swift.trim_end_ms, 9000)

    def test_original_volume_is_capped_at_100(self):
        response = self.post(original_volume=400)
        self.assertEqual(response.status_code, 400)


class FeedStillHidesLocationWithRenderUrlsTests(SwiftTestCase):
    """The media pipeline must not reopen the location rule.

    Worth its own test because `video_url` is served through
    `get_video_url`/`media.playback_url` rather than as a stored column
    directly, and a computed value is exactly the kind of field that could
    start carrying a coordinate if somebody ever named the place in a path.
    """

    def test_no_coordinate_reaches_the_feed_through_a_render_url(self):
        from .testing import find_forbidden

        creator = make_user('creator')
        make_swift(creator, located=True, shared=True, overlay_text='at the park')

        client = APIClient()
        client.force_authenticate(make_user('viewer'))
        response = client.get(reverse('swift-feed'))

        self.assertEqual(find_forbidden(response.json()), [])
        raw = response.content.decode()
        self.assertNotIn('43.234512', raw)
        self.assertNotIn('Kok-Tobe', raw)
