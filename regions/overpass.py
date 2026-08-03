"""Boundary relations straight from OpenStreetMap, when Nominatim has none.

Nominatim answers "what is this place called" extremely well and "what
boundaries enclose this point" only indirectly — through an address hierarchy
that names a level and then has to be searched for by name. Where that
hierarchy is thin, and it is thin across much of the world at sub-city level,
the name never appears and the search never happens, even though the boundary
relation is sitting in OSM perfectly well mapped.

Overpass answers the question directly: `is_in` takes a coordinate and returns
the areas containing it. This module asks that, filters to administrative
boundaries at the levels the country actually uses, and hands the resulting
relation ids back. The geometry then comes from `nominatim.lookup`, because
Overpass returns a relation's border as an unordered bag of ways and assembling
those into rings is a second, avoidable, source of bugs.

It is a *fallback*. It runs only after the cache and both Nominatim stages have
failed, which keeps the volume against a free public service proportionate to
how rare the case is. Its rate limit is its own — Overpass's policy is not
Nominatim's, and sharing a limiter between them would throttle each by the
other's traffic.
"""
import logging
import math
import time

import requests
from django.conf import settings

from . import upstream

logger = logging.getLogger(__name__)

SERVICE = 'overpass'

# Overpass asks for "no more than 2 requests per second, and no heavy use
# without asking". This is a last-resort path behind three caches, so one
# request per second with a single slot is already generous for it.
DEFAULT_MIN_INTERVAL_S = 1.0
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_MAX_QUEUE_WAIT_S = 4.0
# Overpass is slower than Nominatim by design — it is running a query, not a
# lookup — and its own `[timeout:]` is set to match below.
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_QUERY_TIMEOUT_S = 15
# Which relations enclose a point is as stable as the borders themselves.
DEFAULT_CACHE_TTL_S = 7 * 24 * 60 * 60
DEFAULT_MISS_TTL_S = 10 * 60
DEFAULT_ERROR_TTL_S = 3.0
DEFAULT_COALESCE_WAIT_S = DEFAULT_MAX_QUEUE_WAIT_S + DEFAULT_TIMEOUT_S

# Coordinates are rounded before they become a cache key. Two people standing
# in the same building are, for the purpose of "which district relations
# enclose me", asking the identical question — and at five decimal places they
# would each miss the other's cached answer. Four decimals is ~11m, far finer
# than any administrative boundary and coarse enough to actually coalesce.
COORD_PRECISION = 4

_local_clock = [0.0]


class OverpassError(upstream.UpstreamError):
    """Overpass was unreachable, slow, or returned something unusable."""


class OverpassBusy(OverpassError, upstream.UpstreamBusy):
    """No Overpass capacity right now — retryable."""


def _conf(name, default):
    return getattr(settings, f'OVERPASS_{name}', default)


def is_enabled():
    """Whether the direct-OSM fallback is switched on.

    A setting rather than a constant because it is the one stage that talks to
    a service the app does not otherwise depend on. A deployment that would
    rather not add that dependency turns it off and loses one fallback, not the
    feature.
    """
    return bool(_conf('ENABLED', True)) and bool(_conf('URL', ''))


def _ttls():
    return {
        'hit': _conf('CACHE_TTL_S', DEFAULT_CACHE_TTL_S),
        'miss': _conf('MISS_TTL_S', DEFAULT_MISS_TTL_S),
        'error': _conf('ERROR_TTL_S', DEFAULT_ERROR_TTL_S),
        'coalesce_wait': _conf('COALESCE_WAIT_S', DEFAULT_COALESCE_WAIT_S),
    }


# ─── Rate limiting ────────────────────────────────────────────────────────────
def _claim_slot():
    return upstream.claim_slot(
        SERVICE,
        _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S),
        _conf('MAX_CONCURRENCY', DEFAULT_MAX_CONCURRENCY),
        _local_clock,
    )


def _throttle():
    """Block until a request is allowed. Returns the seconds spent waiting."""
    if _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S) <= 0:
        return 0.0

    started = time.monotonic()
    budget = _conf('MAX_QUEUE_WAIT_S', DEFAULT_MAX_QUEUE_WAIT_S)
    deadline = started + budget
    while True:
        if _claim_slot():
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            raise OverpassBusy(
                f'No boundary-relation capacity after {budget:.0f}s.',
                retry_after=max(1, math.ceil(
                    _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S)
                )),
            )
        time.sleep(upstream.POLL_INTERVAL_S)


# ─── Transport ────────────────────────────────────────────────────────────────
def _post(query):
    """One Overpass query. No caching and no coalescing — see `_fetch`."""
    queue_wait = _throttle()
    timeout = _conf('TIMEOUT_S', DEFAULT_TIMEOUT_S)
    started = time.monotonic()

    def elapsed_ms():
        return round((time.monotonic() - started) * 1000)

    try:
        response = requests.post(
            _conf('URL', ''),
            data={'data': query},
            headers={
                'User-Agent': settings.NOMINATIM_USER_AGENT,
                'Accept': 'application/json',
            },
            timeout=timeout,
        )
    except requests.Timeout as exc:
        upstream.event(
            SERVICE, 'upstream_timeout', queue_wait_ms=round(queue_wait * 1000),
            upstream_ms=elapsed_ms(),
        )
        raise OverpassError(f'Boundary relations timed out after {timeout}s.') from exc
    except requests.RequestException as exc:
        upstream.event(
            SERVICE, 'upstream_error', error=type(exc).__name__,
            upstream_ms=elapsed_ms(),
        )
        raise OverpassError(f'Could not reach boundary relations: {exc}') from exc

    if response.status_code != 200:
        upstream.event(
            SERVICE, 'upstream_status', status=response.status_code,
            upstream_ms=elapsed_ms(),
        )
        raise OverpassError(f'Boundary relations returned {response.status_code}.')
    try:
        payload = response.json()
    except ValueError as exc:
        upstream.event(SERVICE, 'upstream_unparseable', upstream_ms=elapsed_ms())
        raise OverpassError('Boundary relations returned a non-JSON body.') from exc

    upstream.event(
        SERVICE, 'upstream_ok', queue_wait_ms=round(queue_wait * 1000),
        upstream_ms=elapsed_ms(),
    )
    return payload


def _fetch(key_params, query):
    return upstream.fetch(SERVICE, 'interpreter', key_params, lambda: _post(query), _ttls())


# ─── Query ────────────────────────────────────────────────────────────────────
def _query(lat, lon, levels):
    """Overpass QL for "administrative relations enclosing this point".

    `is_in` produces the set of areas containing the coordinate; `rel(pivot.a)`
    walks back from each area to the relation it was derived from, which is the
    thing carrying the tags and the id we need. `out tags` deliberately omits
    geometry — that comes from Nominatim, and asking Overpass for it here would
    turn a small response into a very large one.
    """
    pattern = '|'.join(str(int(level)) for level in levels)
    return (
        f'[out:json][timeout:{int(_conf("QUERY_TIMEOUT_S", DEFAULT_QUERY_TIMEOUT_S))}];\n'
        f'is_in({lat:.6f},{lon:.6f})->.a;\n'
        f'rel(pivot.a)["boundary"="administrative"]["admin_level"~"^({pattern})$"];\n'
        f'out tags;'
    )


class Boundary:
    """One administrative relation enclosing the point.

    Just enough to rank and then fetch: the geometry is deliberately absent,
    and `osm_ref` is the handle `nominatim.lookup` takes.
    """

    __slots__ = ('osm_id', 'admin_level', 'name', 'tags')

    def __init__(self, osm_id, admin_level, name, tags):
        self.osm_id = osm_id
        self.admin_level = admin_level
        self.name = name
        self.tags = tags

    @property
    def osm_ref(self):
        return f'R{self.osm_id}'

    def __repr__(self):
        return f'<Boundary {self.osm_ref} level={self.admin_level} {self.name!r}>'


def _admin_level(tags):
    try:
        level = int(tags.get('admin_level'))
    except (TypeError, ValueError):
        return None
    return level if 0 < level < 32 else None


def boundaries_at(lat, lon, levels):
    """Administrative relations containing a point, at the given levels.

    Returns them tightest-first by admin level, which is the only ordering
    Overpass metadata alone supports — everything else about ranking needs the
    geometry, and so happens in the resolver once the polygons are in hand.

    An empty list is a real answer: it means OSM has no administrative boundary
    at those levels there, which is exactly what the "missing from OSM" outcome
    is meant to record.
    """
    if not is_enabled():
        return []
    wanted = [level for level in levels if level]
    if not wanted:
        return []

    # Rounded for the cache key, full precision in the query itself: the answer
    # is identical across a few metres, and paying an upstream call per GPS
    # jitter would defeat the point of a fallback that is meant to be rare.
    key_params = {
        'lat': round(lat, COORD_PRECISION),
        'lon': round(lon, COORD_PRECISION),
        'levels': ','.join(str(level) for level in sorted(wanted)),
    }
    payload = _fetch(key_params, _query(lat, lon, wanted))

    elements = (payload or {}).get('elements') or []
    found = []
    for element in elements:
        if element.get('type') != 'relation':
            continue
        tags = element.get('tags') or {}
        level = _admin_level(tags)
        if level is None or level not in wanted:
            continue
        found.append(Boundary(element.get('id'), level, tags.get('name') or '', tags))

    found.sort(key=lambda b: -b.admin_level)
    upstream.event(
        SERVICE, 'boundaries_found', count=len(found),
        levels=key_params['levels'],
        ids=','.join(b.osm_ref for b in found[:8]) or None,
    )
    return found
