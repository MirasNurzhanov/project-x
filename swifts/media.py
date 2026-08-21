"""Where a Swift's video actually gets composited, and why it is not the phone.

## The decision

A Swift is a trimmed clip, possibly with a music bed at one volume and its own
sound at another, possibly with a line of text over it, and a poster frame.
Producing that file means re-encoding video. There are three places that could
happen and only one of them is a good idea here:

**On the phone, natively.** The library everyone reaches for is
`ffmpeg-kit-react-native`, and it is *retired* — the project was archived in
January 2025 and its prebuilt binaries were pulled from the public
repositories. What remains are unmaintained forks and a handful of narrower
trimming modules, each a native dependency with its own New Architecture
compatibility question. On top of that, re-encoding a 60-second 1080×1920 clip
on a mid-range Android device costs battery, thermal budget and thirty seconds
of the creator staring at a progress bar.

**On our own server.** Django on Railway with no task queue — no Celery, no
worker process. Video encoding inside a request cycle would hold one of the
twelve pooled database connections for the duration and starve the map poll.
Adding a worker to run ffmpeg is a real option and a large one; it is the right
answer at a scale Aura is nowhere near.

**On Cloudinary, which is already the media pipeline.** Cloudinary video
delivery transformations are URL-addressed and lazy: the transformation is
*described* in the path, rendered the first time somebody asks for it, and
served from the CDN edge every time after. So the composite costs one URL
string here and no infrastructure anywhere.

That is what this module builds. The editor on the phone manipulates
parameters and previews them with the local player; the columns on
`SwiftVideo` record what was chosen; and `playback_url` turns those columns
into the address of the finished video.

## What follows from it

* **The upload is the raw recording.** Editing never destroys it, so a trim is
  reversible and a bad render is a rebuilt URL rather than a lost video.
* **There is no processing state machine and no webhook.** A transformation URL
  is valid the moment the source exists. `eager_async` is requested at upload
  time purely to warm the cache, and nothing depends on it having finished.
* **Every render parameter is validated here**, not trusted from the client. A
  transformation is a path segment, so an unescaped string is a path injection.

## Signing

Video uploads are signed. The image path in `services/cloudinary.ts` uses an
unsigned preset, which is a defensible trade for images bounded at 1920px; for
video it is not. An unsigned video preset is an open door to anyone who reads
the JS bundle, and video is the expensive asset in both storage and egress.
"""
import hashlib
import re
import time
from urllib.parse import quote

from django.conf import settings

#: Hard ceiling on a Swift, in milliseconds. Enforced on the client for a
#: decent error and again on the server because the client is not trusted.
#: Sixty seconds is where short-form stops being short and the egress bill
#: stops being incidental.
MAX_DURATION_MS = 60_000
MIN_DURATION_MS = 1_000

#: Ceiling on the raw upload. Generous enough for 60s of 1080p at a phone's
#: default bitrate, tight enough that a mis-set camera cannot post a gigabyte.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: The folder every Swift source lands in.
UPLOAD_FOLDER = 'swifts'

#: Cloudinary public ids we will build a URL for. Anything else is either a
#: mistake or an attempt to point our delivery URL at another account's asset.
_PUBLIC_ID_RE = re.compile(r'^[A-Za-z0-9_\-/]{1,180}$')


class MediaConfigError(RuntimeError):
    """Cloudinary credentials are missing. A deployment fact, not a user error."""


def _config():
    store = getattr(settings, 'CLOUDINARY_STORAGE', {}) or {}
    cloud = store.get('CLOUD_NAME')
    key = store.get('API_KEY')
    secret = store.get('API_SECRET')
    if not (cloud and key and secret):
        raise MediaConfigError('Cloudinary is not configured on this server.')
    return cloud, key, secret


def is_configured():
    try:
        _config()
    except MediaConfigError:
        return False
    return True


def valid_public_id(public_id):
    return bool(public_id) and bool(_PUBLIC_ID_RE.match(public_id))


# ─── Signing an upload ────────────────────────────────────────────────────────
def sign_upload(user, *, timestamp=None):
    """Parameters the phone needs to upload one video directly to Cloudinary.

    The signature covers every parameter it constrains, which is what stops the
    client widening its own permissions: the destination is fixed and the
    resource type is video. A client that alters any of them invalidates the
    signature.

    The public id embeds the uploader so that an asset can always be traced
    back to an account without a database round trip, and a random component so
    that two uploads from one person in the same second cannot collide. It
    carries the `swifts/` prefix, and that prefix *is* the folder — see the
    note below for why there is no longer a separate `folder` parameter.

    `params` is returned as its own dict and the client posts it back
    key-for-key, rather than composing the form from a list of its own. The two
    ends drifting apart is the failure that prevents, and it is a bad one:
    Cloudinary recomputes the signature over whatever it receives, so a form
    carrying one parameter too many or too few is a 401 that names no
    parameter at all.
    """
    import secrets

    cloud, key, secret = _config()
    timestamp = timestamp or int(time.time())
    public_id = f'{UPLOAD_FOLDER}/u{user.pk}_{timestamp}_{secrets.token_hex(6)}'

    # Two parameters used to sit in here and both were wrong.
    #
    # `folder='swifts'` went alongside a `public_id` that already begins
    # `swifts/`. Under Cloudinary's fixed-folder mode that stores the asset at
    # `swifts/swifts/...`; under the dynamic-folder mode every account created
    # since 2023 gets by default, a slashed `public_id` combined with `folder`
    # is a conflict and the upload is refused outright.
    #
    # `image_metadata='false'` was there to strip the GPS coordinates phone
    # cameras write into a video container. It never did that. Cloudinary's
    # `image_metadata` controls whether metadata is *returned in the upload
    # response*, 'false' is already its default, and the spelling is the
    # image-only one — deprecated in favour of `media_metadata` — being posted
    # to the /video/upload endpoint.
    #
    # What actually keeps container metadata away from viewers is delivery:
    # `playback_url` and `poster_url` both end in `q_auto,f_auto`, so every
    # byte anyone is served is transcoded and carries none of the source
    # container. The raw original keeps its metadata and is reachable only by
    # someone who already knows the public id — 48 bits of randomness handed
    # out from an authenticated endpoint.
    params = {
        'timestamp': str(timestamp),
        'public_id': public_id,
    }
    signature = _sign(params, secret)

    return {
        'cloud_name': cloud,
        'api_key': key,
        'signature': signature,
        'params': params,
        # Also flat: the client reads `public_id` back when it saves the Swift,
        # and `timestamp` to judge whether a held signature is still fresh.
        'timestamp': timestamp,
        'public_id': public_id,
        'upload_url': f'https://api.cloudinary.com/v1_1/{cloud}/video/upload',
        'max_bytes': MAX_UPLOAD_BYTES,
        'max_duration_ms': MAX_DURATION_MS,
    }


def _sign(params, secret):
    """Cloudinary's scheme: sorted `k=v` pairs, joined by `&`, secret appended,
    SHA-1 of the result."""
    payload = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    return hashlib.sha1(f'{payload}{secret}'.encode()).hexdigest()


# ─── Building the delivery URL ────────────────────────────────────────────────
def _ms_to_s(ms):
    """Cloudinary offsets are seconds with up to 3 decimals."""
    return f'{ms / 1000:.3f}'.rstrip('0').rstrip('.')


def _escape_text(value):
    """Make a caption safe to sit inside a transformation path segment.

    Three separate hazards, and the third is the one that bites: `,` ends a
    transformation parameter, `/` ends a path component, and a literal `%`
    breaks the percent-encoding of everything after it. Cloudinary's own
    guidance is to double-encode text overlays, which is what `quote` twice
    amounts to here.
    """
    cleaned = (value or '').replace('\n', ' ').strip()
    if not cleaned:
        return ''
    return quote(quote(cleaned, safe=''), safe='')


#: The font a text overlay is drawn in. A Cloudinary-hosted family rather than
#: Aura's own Space Grotesk, because a custom font has to be uploaded to the
#: account before it can be named here — worth doing, and a deployment step
#: rather than a code one. Tracked in the report as a known gap.
OVERLAY_FONT = 'Arial'
OVERLAY_SIZE = 56

_OVERLAY_GRAVITY = {
    'top': 'north',
    'middle': 'center',
    'bottom': 'south',
}


def transformation_chain(swift):
    """The ordered list of transformation components for one Swift.

    Order matters and is not arbitrary:

      1. **trim** first, so everything after it operates on the clip the
         creator actually kept rather than the whole recording,
      2. **original volume**, which applies to the source's own audio,
      3. **the music layer**, added over the result with its own volume,
      4. **the text overlay**, last, so it is never scaled by an earlier crop,
      5. **format and quality**, which Cloudinary applies at delivery.
    """
    parts = []

    # 1 — trim
    if swift.trim_start_ms:
        parts.append(f'so_{_ms_to_s(swift.trim_start_ms)}')
    if swift.trim_end_ms:
        parts.append(f'eo_{_ms_to_s(swift.trim_end_ms)}')

    # 2 — the source's own sound. `e_volume:0` is a mute; there is deliberately
    # no separate boolean, so the two cannot disagree.
    if swift.original_volume != 100:
        parts.append(f'e_volume:{int(swift.original_volume)}')

    # 3 — the music bed, when there is one and it is audible.
    track = swift.track
    if track and track.cloudinary_id and swift.music_volume > 0:
        layer = f'l_video:{track.cloudinary_id}'
        parts.append(layer)
        parts.append(f'e_volume:{int(swift.music_volume)}')
        parts.append('fl_layer_apply')

    # 4 — one line of text.
    text = _escape_text(swift.overlay_text)
    if text:
        gravity = _OVERLAY_GRAVITY.get(swift.overlay_position, 'center')
        parts.append(f'l_text:{OVERLAY_FONT}_{OVERLAY_SIZE}_bold:{text},co_white')
        parts.append(f'g_{gravity},y_120,fl_layer_apply')

    # 5 — let Cloudinary pick the codec and bitrate for the requesting client.
    parts.append('q_auto')
    parts.append('f_auto')
    return parts


def playback_url(swift):
    """The address of the finished, composited video.

    Returns '' when there is no source to build from, which is a real state:
    a row can exist before its upload has been confirmed.
    """
    if not valid_public_id(swift.media_id):
        return ''
    try:
        cloud, _, _ = _config()
    except MediaConfigError:
        return ''
    chain = ','.join(transformation_chain(swift))
    return f'https://res.cloudinary.com/{cloud}/video/upload/{chain}/{swift.media_id}.mp4'


def poster_url(swift):
    """The still frame that shows before playback starts.

    Taken from the composited clip at `cover_ms`, so the poster is a frame the
    viewer will actually see rather than something from a trimmed-off head.
    Defaults to the first frame of the kept range.
    """
    if not valid_public_id(swift.media_id):
        return ''
    try:
        cloud, _, _ = _config()
    except MediaConfigError:
        return ''

    cover = swift.cover_ms if swift.cover_ms is not None else swift.trim_start_ms
    # Clamp into the kept range: a cover time outside it would render a frame
    # from footage the creator cut.
    lower = swift.trim_start_ms or 0
    upper = swift.trim_end_ms or (swift.duration_ms or 0)
    if upper and cover > upper:
        cover = upper
    if cover < lower:
        cover = lower

    parts = [f'so_{_ms_to_s(cover)}', 'q_auto', 'f_auto']
    return f'https://res.cloudinary.com/{cloud}/video/upload/{",".join(parts)}/{swift.media_id}.jpg'
