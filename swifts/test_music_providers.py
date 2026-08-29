"""The music provider layer: mapping, syncing, and the gate a sync must not skip.

The load-bearing assertion here is that **an import never publishes**. Every
other rule in this file follows from it — a sync that could switch tracks on
would make the licence review that `is_active` exists for into a formality.
"""
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SwiftTrack
from .music import registry, sync
from .music.base import ProviderNotConfigured, ProviderTrack
from .music.epidemic import EpidemicSoundProvider
from .testing import SwiftTestCase, make_user

CREDS = {
    'EPIDEMIC_SOUND_CLIENT_ID': 'key-id',
    'EPIDEMIC_SOUND_CLIENT_SECRET': 'key-secret',
}


def provider_track(**extra):
    fields = {
        'external_id': 'ES-1001',
        'title': 'Night Drive',
        'artist': 'Someone',
        'genre': 'Lo-fi',
        'duration_ms': 180_000,
        'audio_url': 'https://cdn.epidemicsound.com/a/1001.mp3',
        'licence': 'Epidemic Sound partner agreement',
    }
    fields.update(extra)
    return ProviderTrack(**fields)


class ProviderTrackTests(SwiftTestCase):
    def test_a_track_without_a_licence_cannot_exist(self):
        """The guard that stops an unvetted import becoming a catalogue row.

        Raised in the dataclass rather than checked in the sync, so there is no
        path — including a future provider written by somebody else — that can
        produce a licence-less track at all.
        """
        with self.assertRaises(ValueError) as caught:
            provider_track(licence='')
        self.assertIn('licence', str(caught.exception))

    def test_a_track_needs_an_external_id(self):
        with self.assertRaises(ValueError):
            provider_track(external_id='')


class EpidemicMappingTests(SwiftTestCase):
    """`_to_track`, which is the one place the API's response shape is assumed.

    Deliberately tolerant of spelling: their newer and older endpoints differ,
    and a mismatch should cost one field rather than the whole sync.
    """

    def setUp(self):
        super().setUp()
        self.provider = EpidemicSoundProvider()

    @override_settings(**CREDS)
    def test_maps_the_modern_shape(self):
        track = self.provider._to_track({
            'id': 'ES-1001',
            'title': 'Night Drive',
            'artists': [{'name': 'A'}, {'name': 'B'}],
            'genres': [{'name': 'Lo-fi'}],
            'length': 180,
            'stems': {'full': {'lqMp3Url': 'https://cdn/x.mp3'}},
            'images': {'default': 'https://cdn/x.jpg'},
        })

        self.assertEqual(track.external_id, 'ES-1001')
        self.assertEqual(track.artist, 'A, B')
        self.assertEqual(track.genre, 'Lo-fi')
        self.assertEqual(track.duration_ms, 180_000)
        self.assertEqual(track.audio_url, 'https://cdn/x.mp3')

    @override_settings(**CREDS)
    def test_maps_a_flatter_shape(self):
        track = self.provider._to_track({
            'trackId': 'ES-2',
            'name': 'Sunrise',
            'artists': 'Solo Artist',
            'genre': 'Ambient',
            'durationMs': 90_000,
            'previewUrl': 'https://cdn/y.mp3',
            'imageUrl': 'https://cdn/y.jpg',
        })

        self.assertEqual(track.title, 'Sunrise')
        self.assertEqual(track.artist, 'Solo Artist')
        self.assertEqual(track.duration_ms, 90_000)

    @override_settings(**CREDS)
    def test_an_item_with_no_id_is_dropped_rather_than_guessed(self):
        self.assertIsNone(self.provider._to_track({'title': 'Nameless'}))

    @override_settings(**CREDS, EPIDEMIC_SOUND_LICENCE='Partner agreement 2026-A')
    def test_the_licence_comes_from_settings(self):
        """Agreement-specific, and the thing somebody reads back in two years."""
        track = self.provider._to_track({'id': 'x', 'title': 'T'})
        self.assertEqual(track.licence, 'Partner agreement 2026-A')


class EpidemicConfigurationTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.provider = EpidemicSoundProvider()

    @override_settings(EPIDEMIC_SOUND_CLIENT_ID='', EPIDEMIC_SOUND_CLIENT_SECRET='')
    def test_missing_credentials_are_named_individually(self):
        """"Missing EPIDEMIC_SOUND_CLIENT_SECRET" is a fix; "not configured"
        is a scavenger hunt."""
        self.assertFalse(self.provider.is_configured())
        self.assertEqual(
            set(self.provider.missing_settings()),
            {'EPIDEMIC_SOUND_CLIENT_ID', 'EPIDEMIC_SOUND_CLIENT_SECRET'},
        )

    @override_settings(EPIDEMIC_SOUND_CLIENT_ID='id', EPIDEMIC_SOUND_CLIENT_SECRET='')
    def test_a_half_configured_provider_names_only_what_is_missing(self):
        self.assertEqual(
            self.provider.missing_settings(), ('EPIDEMIC_SOUND_CLIENT_SECRET',))

    @override_settings(EPIDEMIC_SOUND_CLIENT_ID='', EPIDEMIC_SOUND_CLIENT_SECRET='')
    def test_using_it_unconfigured_raises_rather_than_calling_out(self):
        with self.assertRaises(ProviderNotConfigured):
            self.provider._access_token()

    @override_settings(**CREDS)
    def test_configured(self):
        self.assertTrue(self.provider.is_configured())


class SyncTests(SwiftTestCase):
    def test_a_sync_creates_inactive_tracks(self):
        """**The rule this whole module is built around.**

        An import that published would make the licence review `is_active`
        exists for into a formality.
        """
        result = sync.apply_tracks('epidemic', [provider_track()])

        self.assertEqual(result.created, 1)
        track = SwiftTrack.objects.get()
        self.assertFalse(track.is_active)
        self.assertEqual(track.provider, 'epidemic')
        self.assertEqual(track.provider_track_id, 'ES-1001')
        self.assertEqual(track.source, SwiftTrack.Source.LICENSED)

    def test_an_imported_track_is_not_in_the_picker_until_published(self):
        """The gate, asserted end to end through the API creators actually use."""
        sync.apply_tracks('epidemic', [provider_track()])

        client = APIClient()
        client.force_authenticate(make_user('creator'))
        body = client.get(reverse('swift-tracks')).json()
        self.assertEqual(body['results'], [])

        SwiftTrack.objects.update(is_active=True)
        body = client.get(reverse('swift-tracks')).json()
        self.assertEqual(len(body['results']), 1)

    def test_a_resync_updates_rather_than_duplicating(self):
        sync.apply_tracks('epidemic', [provider_track()])
        sync.apply_tracks('epidemic', [provider_track(title='Night Drive (remaster)')])

        self.assertEqual(SwiftTrack.objects.count(), 1)
        self.assertEqual(SwiftTrack.objects.get().title, 'Night Drive (remaster)')

    def test_a_resync_does_not_unpublish_a_track_somebody_published(self):
        """The failure this would otherwise cause is silent and confusing: a
        track disappears from the picker because an upstream field changed."""
        sync.apply_tracks('epidemic', [provider_track()])
        SwiftTrack.objects.update(is_active=True, priority=10)

        sync.apply_tracks('epidemic', [provider_track(title='Renamed')])

        track = SwiftTrack.objects.get()
        self.assertTrue(track.is_active)
        self.assertEqual(track.priority, 10)
        self.assertEqual(track.title, 'Renamed')

    def test_a_resync_keeps_a_mirrored_cloudinary_id(self):
        """Approx's copy of the audio is Approx's, and re-importing metadata must
        not throw it away."""
        sync.apply_tracks('epidemic', [provider_track()])
        SwiftTrack.objects.update(cloudinary_id='tracks/mirrored')

        sync.apply_tracks('epidemic', [provider_track(title='X')])
        self.assertEqual(SwiftTrack.objects.get().cloudinary_id, 'tracks/mirrored')

    def test_two_providers_may_share_an_external_id(self):
        """The uniqueness is per provider, not global — ids are only unique
        within the library that issued them."""
        sync.apply_tracks('epidemic', [provider_track()])
        sync.apply_tracks('other', [provider_track()])
        self.assertEqual(SwiftTrack.objects.count(), 2)

    def test_tracks_with_no_audio_or_title_are_skipped(self):
        result = sync.apply_tracks('epidemic', [
            provider_track(external_id='a', audio_url=''),
            provider_track(external_id='b', title=''),
            provider_track(external_id='c'),
        ])

        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.created, 1)

    def test_dry_run_writes_nothing(self):
        result = sync.apply_tracks('epidemic', [provider_track()], dry_run=True)

        self.assertEqual(result.created, 1)
        self.assertEqual(SwiftTrack.objects.count(), 0)

    def test_hand_added_tracks_are_untouched_by_a_sync(self):
        """A row somebody typed in has no provider, so nothing matches it."""
        manual = SwiftTrack.objects.create(
            title='Hand made', audio_url='https://cdn/h.mp3',
            source=SwiftTrack.Source.OWNED, licence='Commissioned',
            is_active=True,
        )
        sync.apply_tracks('epidemic', [provider_track()])

        manual.refresh_from_db()
        self.assertTrue(manual.is_active)
        self.assertEqual(manual.provider, '')
        self.assertEqual(SwiftTrack.objects.count(), 2)


class SearchTransportTests(SwiftTestCase):
    """The provider's own request path, with the network stubbed."""

    @override_settings(**CREDS)
    def test_search_maps_a_page_of_results(self):
        provider = EpidemicSoundProvider()
        with patch.object(provider, '_get', return_value={'tracks': [
            {'id': 'a', 'title': 'One', 'length': 60,
             'stems': {'full': {'lqMp3Url': 'https://cdn/a.mp3'}}},
            {'id': 'b', 'title': 'Two', 'length': 90,
             'stems': {'full': {'lqMp3Url': 'https://cdn/b.mp3'}}},
        ]}):
            tracks = provider.search(term='night')

        self.assertEqual([t.title for t in tracks], ['One', 'Two'])
        self.assertEqual(tracks[0].duration_ms, 60_000)

    @override_settings(**CREDS)
    def test_search_survives_an_unrecognised_envelope(self):
        """A shape we do not know is an empty page, not a crash mid-sync."""
        provider = EpidemicSoundProvider()
        with patch.object(provider, '_get', return_value={'unexpected': True}):
            self.assertEqual(provider.search(), [])


class ProviderStatusEndpointTests(SwiftTestCase):
    @override_settings(EPIDEMIC_SOUND_CLIENT_ID='', EPIDEMIC_SOUND_CLIENT_SECRET='')
    def test_status_names_what_is_missing(self):
        rows = registry.status()
        epidemic = next(r for r in rows if r['key'] == 'epidemic')

        self.assertFalse(epidemic['configured'])
        self.assertIn('EPIDEMIC_SOUND_CLIENT_ID', epidemic['missing'])

    def test_the_endpoint_is_staff_only(self):
        """It names environment variables — operational detail a creator has no
        use for and an attacker would enjoy."""
        client = APIClient()
        client.force_authenticate(make_user('creator'))
        self.assertEqual(
            client.get(reverse('swift-music-providers')).status_code, 403)

    def test_staff_can_read_it(self):
        staff = make_user('ops')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])

        client = APIClient()
        client.force_authenticate(staff)
        response = client.get(reverse('swift-music-providers'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            any(r['key'] == 'epidemic' for r in response.json()['results']))


class RegistryTests(SwiftTestCase):
    def test_unknown_provider_raises(self):
        with self.assertRaises(KeyError):
            registry.get('spotify')

    def test_epidemic_is_registered(self):
        self.assertEqual(registry.get('epidemic').key, 'epidemic')
