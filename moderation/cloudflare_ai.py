"""The only module in the project that talks to Cloudflare.

Everything Approx-specific is deliberately absent: this file does not import a
model, does not know what a Swift is, and does not decide whether anything gets
published. It turns bytes into a `ModerationResult` and raises a typed error
when it cannot. `swifts.ai_moderation` is where those results become a
decision, and keeping the two apart is what makes the decision testable without
a network and the client testable without a database.

Three Cloudflare surfaces are used, and only three:

    Media Transformations   ``/cdn-cgi/media/<options>/<source>`` — a URL, not
                            an API. Extracts a still frame (``mode=frame``) or
                            the audio track (``mode=audio``) from a video that
                            is already sitting in R2. This is why no FFmpeg
                            appears anywhere in this project: the transcode
                            happens at Cloudflare's edge, addressed by URL, and
                            Django only ever holds the small JPEG that comes
                            back.

    Workers AI vision       ``POST /accounts/{id}/ai/run/{model}`` with the
                            frame's bytes. Classifies one still image.

    Workers AI transcribe   the same endpoint, a different model, with the
                            audio track's bytes. Optional — see
                            `SWIFT_AI_AUDIO_MODERATION_ENABLED`.

Two rules this module holds to, both of which are security properties rather
than style:

  * **The token is read from settings at call time and never leaves.** It is
    not returned, not stored, not attached to an exception, and not logged.
    `redact` exists because the obvious way to debug an HTTP failure — print
    the request — is the one thing that must never happen here.

  * **Model output is parsed, validated and normalised, never trusted.** The
    vision model is asked for compact JSON and frequently returns prose with
    JSON in the middle of it, or JSON with a confidence of ``"high"`` instead
    of a number, or a category nobody defined. `parse_vision_response` treats
    all of that as *data of unknown quality*: anything it cannot read becomes
    an error, and an error fails closed at the caller rather than passing as a
    safe verdict.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings

from .reasons import Reason

logger = logging.getLogger(__name__)

#: Where Workers AI lives. A setting only so a test can point it at nothing;
#: there is no second deployment of this API.
API_ROOT = 'https://api.cloudflare.com/client/v4'


# ─── Errors ───────────────────────────────────────────────────────────────────
class CloudflareError(Exception):
    """Something went wrong reaching Cloudflare. Never carries credentials.

    `retryable` is the only thing callers branch on. A timeout, a 5xx and a
    429 are worth trying again; a 400 for a malformed request and a 403 for a
    token without the right permission will say the same thing forever, and a
    job that retries those burns quota to learn nothing.
    """

    def __init__(self, message, *, retryable=True, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class MissingCredentials(CloudflareError):
    """`CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is not set.

    Not retryable, and deliberately an exception rather than a silent skip:
    "moderation is switched on and cannot run" has to fail closed, which means
    the caller leaves the content unpublished. A version of this that returned
    "safe, nothing configured" would publish everything the moment a variable
    was mistyped.
    """

    def __init__(self, missing):
        super().__init__(
            f'Cloudflare moderation is enabled but {missing} is not set.',
            retryable=False,
        )


# ─── Results ──────────────────────────────────────────────────────────────────
#: The categories a vision model may report, and the `moderation.reasons.Reason`
#: each one routes to. The prompt names exactly these strings, so anything else
#: coming back is a model that ignored the instruction — `_categories` drops it
#: rather than inventing a bucket, and a result whose categories all get dropped
#: is treated as unsafe-but-uncategorised rather than as safe.
CATEGORY_REASONS = {
    'sexual_content': Reason.NUDITY,
    'nudity': Reason.NUDITY,
    'sexual_exploitation': Reason.MINOR_SAFETY,
    'minor_safety': Reason.MINOR_SAFETY,
    'graphic_violence': Reason.VIOLENCE,
    'weapons_threat': Reason.VIOLENCE,
    'hate_symbols': Reason.HATE,
    'self_harm': Reason.SELF_HARM,
    'other_objectionable': Reason.OTHER,
}

#: Ordered worst-first. When several frames object for several reasons, this is
#: which one the report is filed under — a Swift flagged for both `minor_safety`
#: and `spam` is a minor-safety item, and filing it as spam would sort it into
#: the wrong end of the queue.
REASON_SEVERITY = (
    Reason.MINOR_SAFETY,
    Reason.SELF_HARM,
    Reason.NUDITY,
    Reason.VIOLENCE,
    Reason.HATE,
    Reason.SPAM,
    Reason.PRIVACY,
    Reason.OTHER,
)


@dataclass(frozen=True)
class ModerationResult:
    """What one model call concluded about one piece of media.

    Structured on purpose. The provider's own response — free text, its own
    field names, whatever it decided to include — never leaves this module;
    everything downstream sees this shape and nothing else, which is what makes
    swapping the model a change to one file.

    `safe=False` with an empty `categories` is a real and useful state: the
    model objected but named nothing recognisable. That is *not* the same as
    safe, and the decision policy treats it as uncertain rather than as a
    verdict.
    """

    safe: bool = True
    confidence: float = 0.0
    categories: tuple = field(default_factory=tuple)
    #: Which frame this was, in milliseconds from the start. None for a result
    #: that is not about a frame — a transcript, say.
    at_ms: int = None

    @property
    def reasons(self):
        """The `Reason` values these categories route to, worst first."""
        found = {CATEGORY_REASONS[c] for c in self.categories if c in CATEGORY_REASONS}
        return tuple(r for r in REASON_SEVERITY if r in found)


@dataclass(frozen=True)
class TranscriptResult:
    """What the speech-to-text model heard. Text only — no verdict.

    The verdict is `moderation.text.classify`'s to make, so that a spoken slur
    and a typed one are judged by the same rules rather than by two filters
    that will drift.
    """

    text: str = ''


# ─── Configuration ────────────────────────────────────────────────────────────
def _setting(name, default=None):
    return getattr(settings, name, default)


def credentials():
    """`(account_id, token)`, or raise `MissingCredentials`.

    The only reader of either variable in the entire project. Nothing else
    imports them, nothing else is allowed to, and the return value is used
    immediately and dropped — it is never assigned to a module global where a
    traceback could pick it up.
    """
    account = (_setting('CLOUDFLARE_ACCOUNT_ID') or '').strip()
    token = (_setting('CLOUDFLARE_API_TOKEN') or '').strip()
    if not account:
        raise MissingCredentials('CLOUDFLARE_ACCOUNT_ID')
    if not token:
        raise MissingCredentials('CLOUDFLARE_API_TOKEN')
    return account, token


def is_configured():
    """Whether a call could be made at all. Never raises — for system checks."""
    try:
        credentials()
    except MissingCredentials:
        return False
    return True


def _timeout():
    return float(_setting('CLOUDFLARE_AI_TIMEOUT_S', 30))


def redact(text, limit=300):
    """A provider-supplied string that is safe to log.

    Truncated, and scrubbed of anything shaped like a bearer token. Cloudflare
    does not echo the Authorization header back, so this is belt and braces —
    but the cost of being wrong once is a credential in a log aggregator
    forever, and the cost of the function is four lines.

    Public, and used by `swifts.ai_moderation` as well as here, because the
    defence has to sit at the point where a provider string reaches a log
    rather than only at the point where the string is built. A future code path
    that forgets to build its message through this module is then still
    covered.
    """
    text = str(text or '')[:limit]
    return re.sub(r'(?i)bearer\s+[\w.\-]+', 'Bearer [redacted]', text)


#: Kept for the internal call sites written against the old private name.
_redact = redact


# ─── Media Transformations ────────────────────────────────────────────────────
def transform_base():
    """The origin that serves `/cdn-cgi/media/`, with no trailing slash.

    Defaults to the host the R2 bucket is already published on, because that is
    the one origin guaranteed to hold these videos. It is a separate setting
    because Media Transformations are enabled *per zone*: if the bucket is
    published on an `r2.dev` address — which is Cloudflare's, not the account's
    — there is no zone to enable them on, and this has to point at a custom
    domain instead. See the deployment notes in the README for this feature.
    """
    explicit = (_setting('CLOUDFLARE_MEDIA_TRANSFORM_BASE') or '').strip()
    if explicit:
        return explicit.rstrip('/')
    public = (_setting('R2_PUBLIC_URL') or '').strip()
    if public:
        return public.rstrip('/')
    return ''


def _media_url(source_url, options):
    """`https://<base>/cdn-cgi/media/<options>/<source>`.

    The source stays a full absolute URL inside the path — that is the format
    the service defines, not an oversight. It is percent-encoded except for the
    characters a URL needs to stay a URL, because a source containing a `,`
    would otherwise be read as the end of the options list.
    """
    base = transform_base()
    if not base:
        raise CloudflareError(
            'No origin configured for Media Transformations '
            '(set CLOUDFLARE_MEDIA_TRANSFORM_BASE or R2_PUBLIC_URL).',
            retryable=False,
        )
    if not source_url:
        raise CloudflareError('No source video to transform.', retryable=False)
    return f'{base}/cdn-cgi/media/{options}/{quote(source_url, safe=":/?&=%")}'


def _format_time(ms):
    """Milliseconds as the `time=` option wants them: seconds, one decimal.

    One decimal rather than whole seconds because a short Swift sampled at six
    points has its timestamps less than a second apart, and rounding them to
    integers would collapse six distinct frames into two or three identical
    ones — spending the same money to look at less of the video.
    """
    return f'{max(ms, 0) / 1000:.1f}s'


def fetch_frame(source_url, at_ms, *, width=None):
    """One still JPEG from `source_url`, as bytes.

    `width` is a real cost control rather than a nicety: a vision model is
    charged by the tokens an image becomes, and a 1080×1920 frame becomes a lot
    more of them than a 640-wide one does without telling the model anything
    extra about whether the frame contains nudity.
    """
    width = width or int(_setting('SWIFT_AI_FRAME_WIDTH', 640))
    options = f'mode=frame,time={_format_time(at_ms)},format=jpg,width={width},fit=scale-down'
    return _get_bytes(_media_url(source_url, options), what='frame')


def fetch_audio(source_url, *, duration_s=60):
    """The audio track of `source_url` as m4a bytes.

    Capped at the service's own 60-second ceiling, which is also `swifts.media`'s
    ceiling on a Swift — so in practice this is the whole thing.
    """
    duration = max(1, min(int(duration_s or 60), 60))
    options = f'mode=audio,duration={duration}s'
    return _get_bytes(_media_url(source_url, options), what='audio')


def _get_bytes(url, *, what):
    """GET `url` and return the body, or raise `CloudflareError`.

    No credentials: Media Transformations is a public edge path, and the source
    is a public R2 object. Nothing here has an Authorization header to leak,
    which is a reason to keep frame extraction on this path rather than moving
    it behind an authenticated API.
    """
    try:
        response = requests.get(url, timeout=_timeout())
    except requests.Timeout as exc:
        raise CloudflareError(f'Timed out fetching {what}.', retryable=True) from exc
    except requests.RequestException as exc:
        raise CloudflareError(
            f'Could not fetch {what}: {type(exc).__name__}', retryable=True,
        ) from exc

    if response.status_code != 200:
        # 4xx here is usually a configuration answer, not a transient one:
        # transformations not enabled on the zone, or the source origin not on
        # the allowed list. Retrying will not enable them.
        retryable = response.status_code >= 500 or response.status_code == 429
        raise CloudflareError(
            f'Media transformation returned HTTP {response.status_code} for {what}: '
            f'{_redact(response.text, 200)}',
            retryable=retryable,
            status=response.status_code,
        )
    if not response.content:
        raise CloudflareError(f'Media transformation returned an empty {what}.',
                              retryable=True)
    return response.content


# ─── Workers AI ───────────────────────────────────────────────────────────────
def _run(model, payload):
    """`POST /accounts/{id}/ai/run/{model}`, returning the parsed `result`.

    The one place an Authorization header is constructed. `requests` is given
    an explicit timeout on every call — a hung inference request holding a
    worker thread open is how a moderation feature takes a whole API down.
    """
    account, token = credentials()
    url = f'{API_ROOT}/accounts/{account}/ai/run/{model}'
    try:
        response = requests.post(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=_timeout(),
        )
    except requests.Timeout as exc:
        raise CloudflareError('Workers AI timed out.', retryable=True) from exc
    except requests.RequestException as exc:
        raise CloudflareError(
            f'Workers AI unreachable: {type(exc).__name__}', retryable=True,
        ) from exc

    if response.status_code != 200:
        retryable = response.status_code >= 500 or response.status_code == 429
        raise CloudflareError(
            f'Workers AI returned HTTP {response.status_code}: '
            f'{_redact(response.text)}',
            retryable=retryable,
            status=response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise CloudflareError('Workers AI returned a non-JSON body.',
                              retryable=True) from exc

    if not isinstance(body, dict) or not body.get('success'):
        errors = body.get('errors') if isinstance(body, dict) else None
        raise CloudflareError(
            f'Workers AI reported failure: {_redact(errors)}', retryable=True)

    result = body.get('result')
    if not isinstance(result, dict):
        raise CloudflareError('Workers AI returned no result object.', retryable=True)
    return result


# ─── Vision ───────────────────────────────────────────────────────────────────
def vision_model():
    return _setting(
        'CLOUDFLARE_AI_VISION_MODEL', '@cf/meta/llama-3.2-11b-vision-instruct')


def transcribe_model():
    return _setting('CLOUDFLARE_AI_TRANSCRIBE_MODEL', '@cf/openai/whisper')


#: What the model is asked. Short, closed-vocabulary, and explicitly *not* an
#: essay: a long explanation costs tokens, takes longer, and is harder to parse
#: than the two facts that are actually acted on. The category list is the one
#: `CATEGORY_REASONS` knows, spelled out so the model has no reason to invent a
#: name — and `_categories` still drops anything it invents anyway.
#:
#: Never sent to a client, never stored on a report, never logged. A published
#: filter prompt is a specification for getting past the filter.
VISION_PROMPT = (
    'You are a content-safety classifier for a short-video app. '
    'Look at this single video frame and decide whether it breaks the '
    'community guidelines.\n'
    'Respond with ONE line of JSON and nothing else, in exactly this form:\n'
    '{"unsafe": true or false, "categories": [...], "confidence": 0.0 to 1.0}\n'
    'Use only these category strings: sexual_content, nudity, '
    'sexual_exploitation, minor_safety, graphic_violence, weapons_threat, '
    'hate_symbols, self_harm, other_objectionable.\n'
    'Use an empty categories list when the frame is safe. '
    'confidence is how certain you are of your own answer. '
    'Do not explain. Do not add any text before or after the JSON.'
)

#: The first balanced-looking JSON object in a string. Models routinely wrap
#: the answer in prose or a markdown fence however firmly they are told not to,
#: and refusing those responses would fail closed on a model that actually
#: answered correctly.
_JSON_BLOB = re.compile(r'\{.*?\}', re.DOTALL)

#: Words models reach for instead of a number. Mapped rather than rejected —
#: "high" is a real answer to "how confident are you", it is just not a float.
_WORD_CONFIDENCE = {
    'very high': 0.95, 'high': 0.9, 'certain': 0.95, 'confident': 0.85,
    'medium': 0.6, 'moderate': 0.6, 'unsure': 0.4, 'low': 0.3, 'very low': 0.15,
}


def _confidence(raw):
    """A float in [0, 1], or None when the model said something unusable.

    None matters: it is not 0.0. A confidence of zero would read as "certainly
    safe" to a threshold comparison, and a missing confidence has to be treated
    as uncertainty instead.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        # Some models answer on a 0-100 scale despite the instruction.
        if 1.0 < value <= 100.0:
            value /= 100.0
        return min(max(value, 0.0), 1.0)
    if isinstance(raw, str):
        text = raw.strip().lower().rstrip('%')
        try:
            return _confidence(float(text))
        except ValueError:
            return _WORD_CONFIDENCE.get(text)
    return None


def _categories(raw):
    """The recognised category strings in `raw`, deduplicated and ordered.

    Anything unrecognised is dropped rather than passed through. A category the
    rest of the system has no rule for cannot route a report, cannot be
    filtered on in the admin, and would only ever be a string nobody reads.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    seen = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip().lower().replace(' ', '_').replace('-', '_')
        if name in CATEGORY_REASONS and name not in seen:
            seen.append(name)
    return tuple(seen)


def parse_vision_response(text, *, at_ms=None):
    """Turn one model response into a `ModerationResult`, or raise.

    Raising on unreadable output is the whole design. The alternative — return
    a default `ModerationResult()`, which is `safe=True` — would mean a model
    having a bad day silently approves everything, and the failure would be
    invisible precisely because nothing would look broken.
    """
    # Workers AI can return response as either JSON text or a decoded object.
    # Both formats must pass the same verdict validation below.
    if isinstance(text, dict):
        data = text
    elif isinstance(text, str):
        try:
            data = json.loads(text)
        except ValueError:
            # Keep accepting the model's JSON wrapped in prose or a code fence.
            match = _JSON_BLOB.search(text)
            if not match:
                raise CloudflareError(
                    'Vision model returned no JSON object.', retryable=True)
            try:
                data = json.loads(match.group(0))
            except ValueError as exc:
                raise CloudflareError('Vision model returned unparseable JSON.',
                                      retryable=True) from exc
    else:
        raise CloudflareError(
            'Vision model returned neither text nor a JSON object.', retryable=True)
    if not isinstance(data, dict):
        raise CloudflareError('Vision model returned JSON that is not an object.',
                              retryable=True)

    unsafe = data.get('unsafe')
    if isinstance(unsafe, str):
        unsafe = {
            'true': True, 'yes': True, '1': True, 'unsafe': True,
            'false': False, 'no': False, '0': False, 'safe': False,
        }.get(unsafe.strip().lower())
    if not isinstance(unsafe, bool):
        raise CloudflareError('Vision model gave no usable "unsafe" verdict.',
                              retryable=True)

    categories = _categories(data.get('categories'))
    confidence = _confidence(data.get('confidence'))
    if confidence is None:
        # A verdict with no readable confidence is a verdict the decision
        # policy must treat as uncertain, which is what 0.0 does for an unsafe
        # answer. For a safe answer it is also correct: "safe, but I could not
        # tell you how sure I am" should not clear a Swift on its own.
        confidence = 0.0

    # A model that says "unsafe" and names nothing is still saying unsafe. The
    # empty category tuple is carried through so the policy can see that the
    # objection was uncategorised and route it to a human rather than acting.
    return ModerationResult(
        safe=not unsafe,
        confidence=confidence,
        categories=categories if unsafe else (),
        at_ms=at_ms,
    )


def classify_image(image_bytes, *, at_ms=None, prompt=None):
    """Classify one still frame. Returns a `ModerationResult`.

    The image goes as an array of byte values, which is the shape the Workers
    AI image-to-text schema takes over REST.
    """
    if not image_bytes:
        raise CloudflareError('No image bytes to classify.', retryable=False)
    result = _run(vision_model(), {
        'image': list(image_bytes),
        'prompt': prompt or VISION_PROMPT,
        # Enough for the one line of JSON asked for and no more. A cap is also
        # a cost cap: an unbounded response to a prompt the model decided to
        # answer in prose is billed by the token.
        'max_tokens': 128,
        # Classification, not creative writing. The lowest useful setting so
        # the same frame does not get two different verdicts on two runs.
        'temperature': 0.1,
    })
    return parse_vision_response(result.get('response', ''), at_ms=at_ms)


def transcribe(audio_bytes):
    """Speech in `audio_bytes` as text. Returns a `TranscriptResult`.

    No verdict is formed here. The transcript goes back to
    `moderation.text.classify`, so that what somebody says out loud is judged
    by exactly the rules that judge what they type.
    """
    if not audio_bytes:
        raise CloudflareError('No audio bytes to transcribe.', retryable=False)
    result = _run(transcribe_model(), {'audio': list(audio_bytes)})
    text = result.get('text')
    if not isinstance(text, str):
        raise CloudflareError('Transcription model returned no text.', retryable=True)
    return TranscriptResult(text=text.strip())


# ─── Diagnostics ──────────────────────────────────────────────────────────────
def configuration_summary():
    """What is wired up, for a management command and the system check.

    Deliberately reports *whether* the token is set and never any part of it.
    A "first four characters" convenience here is how a credential ends up in a
    screenshot in a ticket.
    """
    base = transform_base()
    return {
        'enabled': bool(_setting('SWIFT_AI_MODERATION_ENABLED', False)),
        'account_id_set': bool((_setting('CLOUDFLARE_ACCOUNT_ID') or '').strip()),
        'api_token_set': bool((_setting('CLOUDFLARE_API_TOKEN') or '').strip()),
        'vision_model': vision_model(),
        'transcribe_model': transcribe_model(),
        'transform_origin': urlsplit(base).netloc or base,
        'audio_enabled': bool(_setting('SWIFT_AI_AUDIO_MODERATION_ENABLED', False)),
        'timeout_s': _timeout(),
    }
