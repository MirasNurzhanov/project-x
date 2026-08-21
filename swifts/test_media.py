"""The render pipeline: signing, transformation URLs, and the catalogue.

The transformation tests matter more than they look. A Cloudinary
transformation is a *path segment*, so every value that reaches one is
concatenated into a URL — which makes an unvalidated public id or an unescaped
caption a path injection under our own cloud name.
"""
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from . import media
from .models import SwiftTrack, SwiftVideo
from .testing import SwiftTestCase, make_swift, make_user

CLOUD = {
    'CLOUD_NAME': 'testcloud', 'API_KEY': '123', 'API_SECRET': 'shh',
}


def make_track(**extra):
    fields = {
        'title': 'Night Drive', 'artist': 'Aura Sounds', 'genre': 'Lo-fi',
        'audio_url': 'https://cdn.example.com/a/night.mp3',
        'cloudinary_id': 'tracks/night',
        'duration_ms': 120_000,
        'source': SwiftTrack.Source.OWNED,
        'licence': 'Commissioned 2026-01, contract A-441',
        'is_active': True,
    }
    fields.update(extra)
    return SwiftTrack.objects.create(**fields)


@override_settings(CLOUDINARY_STORAGE=CLOUD)
class TransformationTests(SwiftTestCase):
    """What the editor's parameters turn into."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')

    def build(self, **extra):
        return make_swift(self.creator, media_id='swifts/u1_abc', **extra)

    def test_an_unedited_swift_still_gets_quality_and_format(self):
        url = media.playback_url(self.build())
        self.assertIn('q_auto', url)
        self.assertIn('f_auto', url)
        self.assertTrue(url.endswith('/swifts/u1_abc.mp4'))

    def test_trim_becomes_start_and_end_offsets(self):
        url = media.playback_url(self.build(trim_start_ms=2500, trim_end_ms=15_000))
        self.assertIn('so_2.5', url)
        self.assertIn('eo_15', url)

    def test_a_zero_start_is_omitted_rather_than_sent_as_zero(self):
        """Shorter URLs cache better and `so_0` is a no-op."""
        self.assertNotIn('so_', media.playback_url(self.build(trim_start_ms=0)))

    def test_muting_the_original_is_volume_zero(self):
        url = media.playback_url(self.build(original_volume=0))
        self.assertIn('e_volume:0', url)

    def test_full_original_volume_adds_nothing(self):
        self.assertNotIn('e_volume', media.playback_url(self.build(original_volume=100)))

    def test_a_track_becomes_an_audio_layer_with_its_own_volume(self):
        url = media.playback_url(
            self.build(track=make_track(), music_volume=40, original_volume=20))

        self.assertIn('l_video:tracks/night', url)
        self.assertIn('e_volume:40', url)
        self.assertIn('e_volume:20', url)
        self.assertIn('fl_layer_apply', url)

    def test_a_silenced_track_is_not_mixed_in_at_all(self):
        """Zero music volume means no layer, not a layer at zero — the layer
        costs a fetch and a mix for something nobody can hear."""
        url = media.playback_url(self.build(track=make_track(), music_volume=0))
        self.assertNotIn('l_video:', url)

    def test_a_track_with_no_cloudinary_id_is_not_mixed_in(self):
        """Auditionable, not usable. Better than a URL that renders nothing."""
        url = media.playback_url(
            self.build(track=make_track(cloudinary_id=''), music_volume=80))
        self.assertNotIn('l_video:', url)

    def test_overlay_text_is_double_encoded(self):
        url = media.playback_url(self.build(overlay_text='hello world'))
        self.assertIn('l_text:', url)
        # Once for the space, again because Cloudinary decodes the segment.
        self.assertIn('hello%2520world', url)

    def test_overlay_text_cannot_break_out_of_the_path(self):
        """The injection this escaping exists for.

        A raw `/` would end the transformation component and let the rest of
        the caption become path segments of its own.
        """
        url = media.playback_url(
            self.build(overlay_text='a/../../evil,co_red'))

        self.assertNotIn('a/../', url)
        self.assertEqual(url.count('/video/upload/'), 1)
        # Everything after the cloud name and before the id is one component.
        tail = url.split('/video/upload/')[1]
        self.assertEqual(tail.count('/'), 2, tail)  # chain / swifts / id

    def test_overlay_position_maps_to_gravity(self):
        for position, gravity in (('top', 'north'), ('middle', 'center'),
                                  ('bottom', 'south')):
            with self.subTest(position=position):
                url = media.playback_url(
                    self.build(overlay_text='hi', overlay_position=position))
                self.assertIn(f'g_{gravity}', url)

    def test_empty_overlay_adds_no_layer(self):
        self.assertNotIn('l_text', media.playback_url(self.build(overlay_text='   ')))

    def test_transformation_order_is_trim_then_volume_then_music_then_text(self):
        """Order is not cosmetic — see `transformation_chain`."""
        chain = media.transformation_chain(self.build(
            trim_start_ms=1000, original_volume=30,
            track=make_track(), music_volume=50, overlay_text='hi',
        ))
        joined = ' '.join(chain)
        self.assertLess(joined.index('so_1'), joined.index('e_volume:30'))
        self.assertLess(joined.index('e_volume:30'), joined.index('l_video'))
        self.assertLess(joined.index('l_video'), joined.index('l_text'))


@override_settings(CLOUDINARY_STORAGE=CLOUD)
class PosterTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')

    def test_cover_defaults_to_the_start_of_the_trim(self):
        swift = make_swift(self.creator, media_id='swifts/x', trim_start_ms=3000)
        self.assertIn('so_3', media.poster_url(swift))

    def test_cover_is_clamped_into_the_kept_range(self):
        """A cover outside the trim would show a frame the creator cut."""
        swift = make_swift(
            self.creator, media_id='swifts/x',
            trim_start_ms=2000, trim_end_ms=8000, cover_ms=30_000,
        )
        self.assertIn('so_8', media.poster_url(swift))

    def test_poster_is_a_jpg(self):
        swift = make_swift(self.creator, media_id='swifts/x')
        self.assertTrue(media.poster_url(swift).endswith('.jpg'))


class PublicIdValidationTests(SwiftTestCase):
    """The path-injection guard on the id itself."""

    def test_traversal_and_schemes_are_refused(self):
        for bad in ('../../etc/passwd', 'a b', 'x?y', 'http://evil/x',
                    'a,b', 'a;b', '', 'x' * 300):
            with self.subTest(bad=bad):
                self.assertFalse(media.valid_public_id(bad))

    def test_normal_ids_pass(self):
        for good in ('swifts/u1_1700000000_abc123', 'tracks/night', 'a-b_c/d'):
            with self.subTest(good=good):
                self.assertTrue(media.valid_public_id(good))

    def test_the_create_endpoint_refuses_a_bad_media_id(self):
        client = APIClient()
        client.force_authenticate(make_user('creator'))
        response = client.post(reverse('swift-create'), {
            'video_url': 'https://cdn.example.com/v/x.mp4',
            'media_id': '../../../secrets',
            'duration_ms': 5000,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('media_id', response.json())


@override_settings(CLOUDINARY_STORAGE=CLOUD)
class UploadSignatureTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('creator')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_returns_everything_the_client_needs(self):
        body = self.client.get(reverse('swift-upload-signature')).json()

        for key in ('cloud_name', 'api_key', 'timestamp', 'signature', 'params',
                    'public_id', 'upload_url', 'max_bytes', 'max_duration_ms'):
            self.assertIn(key, body)
        self.assertTrue(body['upload_url'].endswith('/video/upload'))

    def test_the_public_id_is_scoped_to_the_uploader(self):
        body = self.client.get(reverse('swift-upload-signature')).json()
        self.assertTrue(body['public_id'].startswith(f'swifts/u{self.user.pk}_'))

    def test_two_signatures_do_not_collide(self):
        first = self.client.get(reverse('swift-upload-signature')).json()
        second = self.client.get(reverse('swift-upload-signature')).json()
        self.assertNotEqual(first['public_id'], second['public_id'])

    def test_the_signed_set_is_exactly_what_cloudinary_will_recompute(self):
        """The upload form is `params`, plus api_key, signature and the file.

        Cloudinary recomputes the signature over every field it receives bar
        those three, so this dict has to *be* the form — one key too many or
        too few and the upload comes back 401 without naming the parameter.
        That is what a client composing its own field list kept getting wrong,
        and why the server now hands over the set instead of describing it.
        """
        body = self.client.get(reverse('swift-upload-signature')).json()
        self.assertEqual(
            set(body['params']),
            {'timestamp', 'public_id'},
        )
        self.assertEqual(body['params']['public_id'], body['public_id'])
        self.assertEqual(body['params']['timestamp'], str(body['timestamp']))

    def test_no_folder_parameter_beside_a_folder_prefixed_public_id(self):
        """Sending both is a conflict, not a duplication.

        The public id already begins `swifts/`. A `folder` parameter on top of
        it nests the asset at `swifts/swifts/...` under fixed-folder accounts
        and is refused outright under dynamic-folder ones, which is the default
        for every account created since 2023.
        """
        body = self.client.get(reverse('swift-upload-signature')).json()
        self.assertNotIn('folder', body['params'])
        self.assertTrue(body['public_id'].startswith('swifts/'))

    def test_no_image_only_parameters_on_the_video_endpoint(self):
        """`image_metadata` used to be signed in here as a metadata strip.

        It was not one. Cloudinary's `image_metadata` says whether metadata
        comes back in the *response*, 'false' is its default, and the spelling
        is the deprecated image-only one being posted to /video/upload. What
        keeps container metadata away from viewers is that delivery always
        transcodes — see the tests around `playback_url`, which is never built
        without `q_auto,f_auto`.
        """
        body = self.client.get(reverse('swift-upload-signature')).json()
        self.assertNotIn('image_metadata', body['params'])
        self.assertNotIn('media_metadata', body['params'])

    def test_signature_is_reproducible_from_the_signed_parameters(self):
        body = self.client.get(reverse('swift-upload-signature')).json()
        expected = media._sign(body['params'], CLOUD['API_SECRET'])
        self.assertEqual(body['signature'], expected)

    def test_an_extra_form_field_breaks_the_signature(self):
        """The property the whole scheme rests on, stated once.

        A client cannot quietly widen what it is allowed to upload, because
        anything it adds to the form changes what Cloudinary recomputes.
        """
        body = self.client.get(reverse('swift-upload-signature')).json()
        widened = dict(body['params'], folder='somewhere-else')
        self.assertNotEqual(
            media._sign(widened, CLOUD['API_SECRET']),
            body['signature'],
        )

    def test_requires_authentication(self):
        self.assertEqual(
            APIClient().get(reverse('swift-upload-signature')).status_code, 401)

    @override_settings(CLOUDINARY_STORAGE={})
    def test_missing_credentials_answer_503_with_a_code(self):
        response = self.client.get(reverse('swift-upload-signature'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'not_configured')


class TrackCatalogueTests(SwiftTestCase):
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

    def test_an_inactive_track_cannot_be_attached_to_a_swift(self):
        """The publish gate, enforced where it can be routed around."""
        draft = make_track(is_active=False)
        response = self.client.post(reverse('swift-create'), {
            'video_url': 'https://cdn.example.com/v/x.mp4',
            'duration_ms': 5000,
            'track': draft.pk,
        }, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('track', response.json())

    def test_an_active_track_can_be_attached(self):
        track = make_track()
        response = self.client.post(reverse('swift-create'), {
            'video_url': 'https://cdn.example.com/v/x.mp4',
            'duration_ms': 5000,
            'track': track.pk,
            'audio_title': track.title,
            'audio_artist': track.artist,
            'uses_original_audio': False,
        }, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['track']['title'], 'Night Drive')

    def test_the_credit_survives_the_track_being_withdrawn(self):
        """A licence lapses; the Swifts that used it keep their attribution."""
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
        payload = {'video_url': 'https://cdn.example.com/v/x.mp4', 'duration_ms': 10_000}
        payload.update(extra)
        return self.client.post(reverse('swift-create'), payload, format='json')

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
        self.assertEqual(response.status_code, 201)

        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.trim_start_ms, 1000)
        self.assertEqual(swift.trim_end_ms, 9000)

    def test_volumes_are_capped_at_100(self):
        response = self.post(music_volume=400)
        self.assertEqual(response.status_code, 400)


@override_settings(CLOUDINARY_STORAGE=CLOUD)
class FeedStillHidesLocationWithRenderUrlsTests(SwiftTestCase):
    """The render pipeline must not reopen the location rule.

    Worth its own test because `video_url` is now *computed*, and a computed
    URL is exactly the kind of field that could start carrying a coordinate if
    somebody ever decided to name the place in the file path.
    """

    def test_no_coordinate_reaches_the_feed_through_a_render_url(self):
        from .testing import find_forbidden

        creator = make_user('creator')
        make_swift(
            creator, located=True, shared=True,
            media_id='swifts/x', overlay_text='at the park',
        )

        client = APIClient()
        client.force_authenticate(make_user('viewer'))
        response = client.get(reverse('swift-feed'))

        self.assertEqual(find_forbidden(response.json()), [])
        raw = response.content.decode()
        self.assertNotIn('43.234512', raw)
        self.assertNotIn('Kok-Tobe', raw)
