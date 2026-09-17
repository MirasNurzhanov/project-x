"""Shared plumbing for the outside services this app asks about boundaries.

There are two of them now — Nominatim for names and polygons, Overpass for
"which boundary relations contain this point" — and they have different usage
policies, so their rate limits stay separate and live with each service. What
they share is the part that is easy to get subtly wrong: a response cache, a
leader election so concurrent identical questions cost one request, and the
distinction between an answer that found nothing and a request that failed.

That distinction is the whole reason this is a module rather than a decorator.
A 502 cached as "no district is mapped here" is indistinguishable, at every
later layer, from OSM genuinely having no polygon — and one of those is a fact
about the world while the other is a fact about the last ten seconds.
"""
import hashlib
import json
import logging
import math
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

# How often a waiting caller re-checks, whether for a slot or for a leader's
# answer. Small enough to feel immediate, large enough not to spin.
POLL_INTERVAL_S = 0.15

_RESULT_KEY = 'regions:upstream:result'
_LEADER_KEY = 'regions:upstream:leader'


class UpstreamError(Exception):
    """An upstream was unreachable, slow, or returned something unusable."""


class UpstreamBusy(UpstreamError):
    """No capacity right now — the caller should try again shortly.

    Distinct from its parent because it is the one failure that is purely about
    load rather than about the upstream being broken, so it deserves a
    Retry-After rather than a bare "try again later". Never cached: a caller a
    second later may well find a free slot.
    """

    def __init__(self, message, retry_after=1):
        super().__init__(message)
        self.retry_after = retry_after


# ─── Structured logging ───────────────────────────────────────────────────────
def event(service, name, /, **fields):
    """One structured line per notable step of a lookup.

    Deliberately flat and greppable: `nominatim cache_hit path=search ...`.
    The same fields go out under a single `upstream` key in `extra`, so a JSON
    formatter emits them as structure rather than as a prose message.

    The two leading parameters are positional-only so that a caller can log a
    field genuinely called `name` or `service` — which region lookups do, all
    the time — without colliding with the signature.
    """
    safe = {'path', 'status', 'status_code', 'found', 'count', 'ttl_s',
            'waited_ms', 'upstream_ms', 'kind', 'address_keys', 'country_code'}
    present = {k: v for k, v in fields.items() if k in safe and v is not None}
    logger.info(
        '%s %s %s',
        service,
        name,
        ' '.join(f'{k}={v}' for k, v in present.items()),
        extra={'upstream': {'service': service, 'event': name, **present}},
    )


# ─── Rate limiting ────────────────────────────────────────────────────────────
def claim_slot(service, interval, slots, local_clock):
    """Try to take a request slot. True when one is ours.

    `cache.add` is atomic, which is the whole point: whoever adds the key wins
    and proceeds, everyone else waits. A plain get/set would let two workers
    read the same timestamp and both fire.

    `local_clock` is a one-element mutable holding the caller's process-local
    fallback deadline, used only when the shared cache is unreachable. Less
    strict than the real thing — each worker would keep its own clock — but the
    alternative is either a 500 or an unthrottled call, and both are worse.
    """
    # Django's Redis backend floors a timeout to whole seconds, so a 1.1s lease
    # asked for literally would free after 1s and quietly overrun the policy.
    # Rounding up is the safe direction: slower than allowed, never faster.
    lease = math.ceil(interval)
    try:
        for slot in range(max(1, int(slots))):
            if cache.add(f'regions:{service}:slot:{slot}', '1', timeout=lease):
                return True
        return False
    except Exception:
        logger.warning(
            'Rate-limit cache unavailable for %s; falling back to a local clock.',
            service,
        )
        now = time.monotonic()
        if now < local_clock[0]:
            return False
        local_clock[0] = now + interval
        return True


# ─── Cache and coalescing ─────────────────────────────────────────────────────
def cache_key(service, path, params):
    """A stable key for one exact question.

    Hashed rather than spelled out because a place name can be long, non-ASCII
    and full of characters a cache backend will reject in a key.
    """
    canonical = json.dumps(
        [service, path, sorted((str(k), str(v)) for k, v in params.items())],
        separators=(',', ':'),
    )
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]
    return f'{_RESULT_KEY}:{digest}'


def found_nothing(payload):
    """Whether a well-formed answer nonetheless found nothing.

    Worth distinguishing from an error: it is a real answer and deserves to be
    cached, just not for as long — OSM coverage is the thing most likely to
    have changed by the time somebody asks again.
    """
    if payload is None:
        return True
    if isinstance(payload, list):
        return not payload
    if isinstance(payload, dict):
        if 'elements' in payload:          # Overpass shape
            return not payload['elements']
        return bool(payload.get('error')) or not payload
    return False


def _get(key):
    try:
        return cache.get(key)
    except Exception:
        logger.warning('Response cache unavailable; falling through to upstream.')
        return None


def _set(key, value, ttl):
    try:
        cache.set(key, value, ttl)
    except Exception:
        logger.warning('Response cache unavailable; answer not cached.')


def _release(key):
    """Drop a leadership lease. Best effort — it expires on its own anyway."""
    try:
        cache.delete(key)
    except Exception:
        pass


def _await_leader(key, service, path, wait_s):
    """Wait for whichever caller is already asking this question.

    Returns the leader's envelope, or None if it did not produce one in time.
    Polling a shared cache rather than waiting on an in-process lock is what
    makes this work across gunicorn workers, which is where the duplicate
    requests actually come from — a single process would have coalesced them in
    a dict.
    """
    started = time.monotonic()
    deadline = started + wait_s
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        envelope = _get(key)
        if envelope is not None:
            event(
                service, 'coalesced', path=path,
                waited_ms=round((time.monotonic() - started) * 1000),
            )
            return envelope
    event(
        service, 'coalesce_timeout', path=path,
        waited_ms=round((time.monotonic() - started) * 1000),
    )
    return None


def _unwrap(envelope, service, path, source):
    """Turn a cached envelope back into a payload, or re-raise its error."""
    if 'error' in envelope:
        event(service, f'{source}_error', path=path)
        raise UpstreamError(envelope['error'])
    event(service, source, path=path, found=not envelope.get('missing', False))
    return envelope['payload']


def fetch(service, path, params, transport, ttls):
    """An answer, from cache, from a concurrent caller, or from `transport`.

    `transport` is a zero-argument callable that performs the actual request.
    Keeping it a callable rather than doing the request here is what lets each
    service own its own rate limit, headers and error mapping while sharing all
    of this.

    `ttls` supplies `hit`, `miss`, `error` and `coalesce_wait` in seconds.
    """
    key = cache_key(service, path, params)

    envelope = _get(key)
    if envelope is not None:
        return _unwrap(envelope, service, path, 'cache_hit')
    event(service, 'cache_miss', path=path)

    leader_key = f'{_LEADER_KEY}:{key}'
    try:
        leading = cache.add(leader_key, '1', timeout=math.ceil(ttls['coalesce_wait']))
    except Exception:
        # Without a working cache there is no way to elect a leader. Every
        # caller then fetches for itself, which the rate limiter still holds to
        # policy — slower, but never louder.
        leading = True

    if leading:
        # The leader may have finished between our miss above and our claim
        # here, in which case its answer is already waiting and there is
        # nothing to ask.
        envelope = _get(key)
        if envelope is not None:
            _release(leader_key)
            return _unwrap(envelope, service, path, 'cache_hit')
    else:
        envelope = _await_leader(key, service, path, ttls['coalesce_wait'])
        if envelope is not None:
            return _unwrap(envelope, service, path, 'coalesced')
        # The leader died, or is slower than we are willing to wait. Falling
        # through is safe: the rate limiter is what protects the upstream, and
        # it is still in front of us. We do not take over the lease — it is not
        # ours to release.

    try:
        payload = transport()
    except UpstreamBusy:
        # Capacity, not brokenness. Nothing worth remembering.
        raise
    except UpstreamError as exc:
        # A transient failure is never a cached miss. It does get a marker
        # measured in seconds, so a queue of callers behind one outage fails
        # fast instead of each burning a rate-limit slot to rediscover it.
        _set(key, {'error': str(exc)}, ttls['error'])
        raise
    finally:
        if leading:
            _release(leader_key)

    missing = found_nothing(payload)
    ttl = ttls['miss'] if missing else ttls['hit']
    _set(key, {'payload': payload, 'missing': missing}, ttl)
    event(service, 'cached', path=path, found=not missing, ttl_s=ttl)
    return payload
