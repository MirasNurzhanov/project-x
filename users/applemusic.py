"""The Apple Music catalogue, proxied.

Why the server and not the phone: reaching api.music.apple.com needs a
*developer token*, an ES256 JWT signed with a private key Apple issues once and
will not reissue. A key shipped inside an app binary is a published key, and it
identifies the whole developer account rather than a user — so the key stays
here and the phone gets an endpoint.

What a zone can actually play is the 30-second preview, and that is a ceiling
rather than a shortcut. Full-length playback requires MusicKit plus the
*listener's* own Apple Music subscription; someone walking past a zone has
neither, so a preview is the only thing that can legally reach them. Every
song this module returns therefore carries `preview_url`, and songs without one
are dropped — they would be a track you can pick and nobody can hear.
"""

import time

import jwt
import requests
from django.conf import settings

API_ROOT = 'https://api.music.apple.com/v1'

#: Apple caps a developer token at six months. Well under it, so a token minted
#: at deploy time is never close to the limit, and re-minting stays cheap.
TOKEN_TTL_S = 60 * 60 * 24 * 30  # 30 days
#: Re-mint this long before expiry rather than at it, so no request is ever the
#: one that discovers the token died mid-flight.
TOKEN_REFRESH_MARGIN_S = 60 * 60 * 24

#: Apple is a third party on the far side of the internet from a user who is
#: tapping keys. Search is interactive, so it fails fast rather than hanging.
TIMEOUT_S = 6


class AppleMusicError(Exception):
    """Apple could not answer.

    Carries an HTTP status for the view to mirror, and a `code` naming *which*
    failure this is. The status alone cannot: "no credentials set", "the key
    will not sign" and "Apple rejected the token" are all 503, and they are
    three completely different things to go and fix. Without the code the app
    can only say "unavailable", which sent at least one person hunting for a
    missing variable that was in fact present and wrong.
    """

    def __init__(self, message, status=502, code='unavailable'):
        super().__init__(message)
        self.status = status
        self.code = code


class AppleMusicUnavailable(AppleMusicError):
    """No credentials configured. A deployment fact, not a user error."""

    def __init__(self, missing=()):
        names = ', '.join(missing)
        super().__init__(
            f'Apple Music is not configured on this server. Missing: {names}.'
            if names else 'Apple Music is not configured on this server.',
            status=503,
            code='not_configured',
        )


#: Every variable that has to be present, in the order a person sets them.
REQUIRED_SETTINGS = (
    'APPLE_MUSIC_TEAM_ID',
    'APPLE_MUSIC_KEY_ID',
    'APPLE_MUSIC_PRIVATE_KEY',
)


def missing_settings():
    """Which of the three are absent or blank.

    Named rather than counted: "missing APPLE_MUSIC_TEAM_ID" is a fix, and
    "not configured" is a scavenger hunt across three variables that are set
    in three different places in the Apple portal.
    """
    return tuple(
        name for name in REQUIRED_SETTINGS
        if not str(getattr(settings, name, '') or '').strip()
    )


def is_configured():
    return not missing_settings()


# ─── Developer token ──────────────────────────────────────────────────────────
# Module-level rather than cached in Redis: it is derived from settings, so
# every worker can mint its own for the price of one signature, and a worker
# restart is not a cache miss anyone pays for.
_token = None
_token_expires_at = 0.0


def developer_token():
    """A valid developer token, minted on demand and reused until it nears expiry."""
    global _token, _token_expires_at

    absent = missing_settings()
    if absent:
        raise AppleMusicUnavailable(absent)

    now = time.time()
    if _token and now < _token_expires_at - TOKEN_REFRESH_MARGIN_S:
        return _token

    issued = int(now)
    expires = issued + TOKEN_TTL_S
    # The private key arrives through the environment, where a real newline
    # rarely survives — `\n` written literally is the usual shape of a P8 in a
    # Railway variable, and a PEM without line breaks will not parse.
    private_key = settings.APPLE_MUSIC_PRIVATE_KEY.replace('\\n', '\n').strip()

    try:
        _token = jwt.encode(
            {'iss': settings.APPLE_MUSIC_TEAM_ID, 'iat': issued, 'exp': expires},
            private_key,
            algorithm='ES256',
            headers={'kid': settings.APPLE_MUSIC_KEY_ID, 'alg': 'ES256'},
        )
    except Exception as exc:  # noqa: BLE001 — a bad key is a config error, not a 500
        raise AppleMusicError(
            'APPLE_MUSIC_PRIVATE_KEY is not a usable signing key — it should be '
            'the whole .p8 file including the BEGIN and END lines. '
            f'({exc})',
            status=503,
            code='bad_key',
        ) from exc

    _token_expires_at = expires
    return _token


def _get(path, params=None):
    try:
        response = requests.get(
            f'{API_ROOT}{path}',
            params=params or {},
            headers={'Authorization': f'Bearer {developer_token()}'},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise AppleMusicError(f'Could not reach the music catalogue: {exc}') from exc

    if response.status_code == 401 or response.status_code == 403:
        # Almost always an expired key or a team/kid mismatch, and retrying with
        # the same token would only repeat it — so the cached one is dropped.
        global _token, _token_expires_at
        _token, _token_expires_at = None, 0.0
        raise AppleMusicError(
            'Apple rejected this server\'s token. The key, APPLE_MUSIC_KEY_ID and '
            'APPLE_MUSIC_TEAM_ID do not belong together, or the key has been '
            'revoked or has no MusicKit service enabled.',
            status=503,
            code='rejected',
        )
    if response.status_code == 404:
        raise AppleMusicError('That track is no longer in the catalogue.', status=404)
    if response.status_code >= 400:
        raise AppleMusicError(f'The music catalogue returned an error ({response.status_code}).')

    try:
        return response.json()
    except ValueError as exc:
        raise AppleMusicError('The music catalogue sent back something unexpected.') from exc


# ─── Shaping ──────────────────────────────────────────────────────────────────
def artwork_url(artwork, size=300):
    """Apple sends a template — "…/{w}x{h}bb.jpg" — not an image.

    Substituted here so the client never has to know the convention, and at a
    size a phone list actually renders rather than the 3000px original.
    """
    template = (artwork or {}).get('url')
    if not template:
        return ''
    return template.replace('{w}', str(size)).replace('{h}', str(size))


def _song(item):
    """One catalogue song, flattened to what a zone needs. None if unplayable."""
    attrs = item.get('attributes') or {}
    previews = attrs.get('previews') or []
    preview_url = next((p.get('url') for p in previews if p.get('url')), '')
    if not preview_url:
        return None

    return {
        'id': item.get('id') or '',
        'title': attrs.get('name') or '',
        'artist': attrs.get('artistName') or '',
        'album': attrs.get('albumName') or '',
        'artwork': artwork_url(attrs.get('artwork')),
        'preview_url': preview_url,
        'duration_ms': attrs.get('durationInMillis'),
    }


def _storefront(value=None):
    code = (value or getattr(settings, 'APPLE_MUSIC_STOREFRONT', 'us') or 'us').strip().lower()
    # Storefronts are two-letter country codes; anything else is a path
    # injection wearing a query parameter's clothes.
    return code if len(code) == 2 and code.isalpha() else 'us'


# ─── Public API ───────────────────────────────────────────────────────────────
def search_songs(term, storefront=None, limit=25):
    """Songs matching `term`, newest Apple ranking first, previewable only."""
    term = (term or '').strip()
    if not term:
        return []

    payload = _get(
        f'/catalog/{_storefront(storefront)}/search',
        {'term': term, 'types': 'songs', 'limit': max(1, min(int(limit or 25), 25))},
    )
    data = ((payload.get('results') or {}).get('songs') or {}).get('data') or []
    return [song for song in (_song(item) for item in data) if song]


def get_song(song_id, storefront=None):
    """One song by catalogue id — the authoritative read before storing a pick.

    The client sends an id and nothing else; the title, artwork and preview URL
    that get saved come from here, so a phone cannot write arbitrary URLs into
    a field the map will later hand to an audio player.
    """
    song_id = (song_id or '').strip()
    if not song_id:
        raise AppleMusicError('No track was chosen.', status=400)

    payload = _get(f'/catalog/{_storefront(storefront)}/songs/{song_id}')
    data = payload.get('data') or []
    song = _song(data[0]) if data else None
    if not song:
        raise AppleMusicError(
            'That track has no preview, so a zone cannot play it.', status=400,
        )
    return song
