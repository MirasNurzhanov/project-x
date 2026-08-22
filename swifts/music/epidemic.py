"""Epidemic Sound, as a catalogue source for Swifts.

## Read this before wiring it up

**Epidemic Sound's ordinary subscription does not cover this use.** A personal
or commercial subscription licenses *you* to put their music in *your* content.
Aura is a platform whose users attach music to videos Aura then redistributes,
which is a different thing and needs their **Partner / API agreement**. That
agreement is what issues the credentials below; there is no self-serve key that
makes this legal, and a subscription login is not a substitute for one.

So the honest state of this module: the transport, the token handling, the
sync path and the credential plumbing are all real and finished. What is
*provisional* is the exact request shape — endpoint paths and the JSON field
names in `_to_track` — because those come from partner documentation issued
with the agreement, and I have not had that document in front of me. Every one
of them is a setting or a single mapping function, so correcting them is
configuration or a five-line edit rather than a rewrite. They are marked
`CONFIRM:` below.

## How it fits

This provider does not serve the picker. It *syncs* into `SwiftTrack`, and the
picker reads Aura's own table — see `swifts/music/base.py` for why. Nothing a
creator does causes a request to Epidemic Sound.
"""
import time

import requests
from django.conf import settings

from .base import MusicProvider, MusicProviderError, ProviderNotConfigured, ProviderTrack

#: Every credential that has to be present, in the order a person sets them.
REQUIRED_SETTINGS = (
    'EPIDEMIC_SOUND_CLIENT_ID',
    'EPIDEMIC_SOUND_CLIENT_SECRET',
)

#: Interactive enough to fail fast — a sync is a background job, but a
#: configuration check runs while somebody is looking at a screen.
TIMEOUT_S = 15

#: Re-mint this long before expiry, so no request is the one that discovers the
#: token died mid-flight. Same margin logic as `users.applemusic`.
TOKEN_REFRESH_MARGIN_S = 60


def _setting(name, default=''):
    return str(getattr(settings, name, default) or '').strip()


class EpidemicSoundProvider(MusicProvider):
    key = 'epidemic'
    label = 'Epidemic Sound'

    # Module-level rather than cached in Redis, matching `users.applemusic`:
    # the token is derived from settings, every worker can mint its own for the
    # price of one request, and a restart is not a cache miss anyone pays for.
    _token = None
    _token_expires_at = 0.0

    # ── Configuration ────────────────────────────────────────────────────────
    def missing_settings(self):
        return tuple(name for name in REQUIRED_SETTINGS if not _setting(name))

    def is_configured(self):
        return not self.missing_settings()

    @property
    def base_url(self):
        return _setting(
            'EPIDEMIC_SOUND_BASE_URL',
            'https://partner-content-api.epidemicsound.com',
        ).rstrip('/')

    @property
    def token_url(self):
        return _setting(
            'EPIDEMIC_SOUND_TOKEN_URL',
            'https://partner-content-api.epidemicsound.com/v0/token',
        )

    # ── Auth ─────────────────────────────────────────────────────────────────
    def _access_token(self):
        """A valid bearer token, minted on demand and reused until it nears expiry.

        CONFIRM: the token endpoint and the exact credential grant. This sends
        the client id and secret as a JSON body, which is what their partner
        flow uses; if your agreement documents `client_credentials` form-encoded
        instead, that is the one line to change.
        """
        missing = self.missing_settings()
        if missing:
            raise ProviderNotConfigured(self.label, missing)

        now = time.time()
        if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._token

        try:
            response = requests.post(
                self.token_url,
                json={
                    'accessKeyId': _setting('EPIDEMIC_SOUND_CLIENT_ID'),
                    'accessKeySecret': _setting('EPIDEMIC_SOUND_CLIENT_SECRET'),
                },
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise MusicProviderError(f'Could not reach Epidemic Sound: {exc}') from exc

        if response.status_code in (401, 403):
            raise MusicProviderError(
                'Epidemic Sound rejected these credentials. Check '
                'EPIDEMIC_SOUND_CLIENT_ID and EPIDEMIC_SOUND_CLIENT_SECRET, and '
                'that the partner agreement covering them is active.',
                code='rejected',
                status=503,
            )
        if response.status_code >= 400:
            raise MusicProviderError(
                f'Epidemic Sound returned {response.status_code} for the token request.'
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MusicProviderError('Epidemic Sound sent something that is not JSON.') from exc

        token = payload.get('accessToken') or payload.get('access_token')
        if not token:
            raise MusicProviderError(
                'Epidemic Sound returned no access token.', code='rejected', status=503,
            )

        # `expiresIn` is seconds when present; an hour is their documented
        # default and a safe floor if the field is absent.
        ttl = int(payload.get('expiresIn') or payload.get('expires_in') or 3600)
        type(self)._token = token
        type(self)._token_expires_at = time.time() + ttl
        return token

    def _get(self, path, params=None):
        try:
            response = requests.get(
                f'{self.base_url}{path}',
                params=params or {},
                headers={'Authorization': f'Bearer {self._access_token()}'},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise MusicProviderError(f'Could not reach Epidemic Sound: {exc}') from exc

        if response.status_code in (401, 403):
            # Almost always an expired token; retrying with the same one would
            # only repeat it, so the cached copy is dropped.
            type(self)._token, type(self)._token_expires_at = None, 0.0
            raise MusicProviderError(
                'Epidemic Sound rejected this server\'s token.',
                code='rejected', status=503,
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise MusicProviderError(f'Epidemic Sound returned {response.status_code}.')

        try:
            return response.json()
        except ValueError as exc:
            raise MusicProviderError('Epidemic Sound sent something that is not JSON.') from exc

    # ── Mapping ──────────────────────────────────────────────────────────────
    @property
    def licence_text(self):
        """The licence recorded against every track imported from here.

        Deliberately a *setting* rather than a literal: the exact wording that
        makes a track safe to serve comes from the partner agreement, it differs
        between agreements, and it is the thing somebody will need to read back
        in the admin two years from now. The default names the agreement rather
        than claiming a specific one.
        """
        return _setting(
            'EPIDEMIC_SOUND_LICENCE',
            'Epidemic Sound partner agreement',
        )

    def _to_track(self, item):
        """One API item to a `ProviderTrack`.

        CONFIRM: every field name below. This is the single place the response
        shape is assumed, and it is deliberately tolerant — each field falls
        back through the spellings their APIs have used — so a mismatch loses
        one field rather than the whole sync.
        """
        if not isinstance(item, dict):
            return None

        external_id = str(item.get('id') or item.get('trackId') or '').strip()
        if not external_id:
            return None

        # Artists arrive as a list of objects on their newer endpoints and as a
        # plain string on older ones.
        artists = item.get('artists') or item.get('creatives') or []
        if isinstance(artists, list):
            names = [
                a.get('name') if isinstance(a, dict) else str(a)
                for a in artists
            ]
            artist = ', '.join(n for n in names if n)
        else:
            artist = str(artists or '')

        genres = item.get('genres') or []
        if isinstance(genres, list) and genres:
            first = genres[0]
            genre = first.get('name') if isinstance(first, dict) else str(first)
        else:
            genre = str(item.get('genre') or '')

        length_s = item.get('length') or item.get('lengthInSeconds') or 0
        duration_ms = int(float(length_s) * 1000) if length_s else int(
            item.get('durationMs') or 0
        )

        return ProviderTrack(
            external_id=external_id,
            title=str(item.get('title') or item.get('name') or '').strip(),
            artist=artist.strip(),
            genre=(genre or '').strip(),
            duration_ms=duration_ms,
            audio_url=self._audio_url(item),
            artwork_url=self._artwork_url(item),
            licence=self.licence_text,
            # Epidemic's partner terms generally do not require a per-track
            # credit line the way an open licence does, so this stays blank
            # unless the agreement says otherwise. It is a setting for the same
            # reason `licence_text` is.
            attribution=_setting('EPIDEMIC_SOUND_ATTRIBUTION'),
            extra={'raw_id': external_id},
        )

    def _audio_url(self, item):
        """CONFIRM. Their download/stream URLs are short-lived, which is why
        mirroring exists as a follow-up: a URL that expires is fine to audition
        with and useless as the thing a published Swift plays."""
        for key in ('waveformUrl', 'previewUrl', 'audioUrl', 'url'):
            value = item.get(key)
            if isinstance(value, str) and value.startswith('http'):
                return value
        stems = item.get('stems') or {}
        if isinstance(stems, dict):
            full = stems.get('full') or {}
            if isinstance(full, dict):
                url = full.get('lqMp3Url') or full.get('url')
                if isinstance(url, str):
                    return url
        return ''

    def _artwork_url(self, item):
        images = item.get('images') or {}
        if isinstance(images, dict):
            for key in ('default', 'l', 'm', 's'):
                value = images.get(key)
                if isinstance(value, str) and value.startswith('http'):
                    return value
        cover = item.get('coverArt') or item.get('imageUrl')
        return cover if isinstance(cover, str) else ''

    # ── The interface ────────────────────────────────────────────────────────
    def search(self, term='', limit=50, offset=0):
        """CONFIRM: the search path and its pagination parameters."""
        path = _setting('EPIDEMIC_SOUND_SEARCH_PATH', '/v0/tracks/search')
        payload = self._get(path, {
            'term': term or '',
            'limit': max(1, min(int(limit or 50), 100)),
            'offset': max(0, int(offset or 0)),
        }) or {}

        items = (
            payload.get('tracks')
            or payload.get('items')
            or payload.get('results')
            or []
        )
        tracks = []
        for item in items:
            try:
                track = self._to_track(item)
            except ValueError:
                # A track we cannot state a licence for is skipped rather than
                # imported — see `ProviderTrack.__post_init__`.
                continue
            if track:
                tracks.append(track)
        return tracks

    def fetch(self, external_id):
        path = _setting('EPIDEMIC_SOUND_TRACK_PATH', '/v0/tracks/{id}')
        payload = self._get(path.format(id=external_id))
        if not payload:
            return None
        item = payload.get('track') or payload
        try:
            return self._to_track(item)
        except ValueError:
            return None
