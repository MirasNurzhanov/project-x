"""OpenStreetMap boundary lookups, cached into `Region` rows.

This module is the only thing in the codebase that talks to Nominatim, and it
exists because a zone that follows a city's real border needs that border from
somewhere. The rules it has to respect:

  * **At most one request per second, globally.** That is Nominatim's published
    usage policy, and exceeding it gets an application blocked rather than
    throttled. `_throttle` serialises every call through the Django cache, so
    the limit holds across gunicorn workers and not merely within one process.
  * **Identify yourself.** A request without a real User-Agent is refused.
  * **Never bulk-geocode.** Every lookup here is a direct answer to one person
    picking one place, and the result is cached forever after, so the same city
    is fetched once for the whole userbase.

`NOMINATIM_URL` is a setting on purpose: the public instance is fine at this
size, and swapping in a self-hosted or commercial endpoint later should not
require touching this code. Everything that shapes load — the interval between
requests, how many may be in flight, how long a caller will queue, how long
answers are kept — is a setting for the same reason, so a self-hosted instance
can raise its own limits without a code change.

Three layers stand between a caller and the network, in order:

  1. **Response cache.** Keyed on the exact path and parameters. Successful
     answers live for `NOMINATIM_CACHE_TTL_S`; answers that found nothing live
     for a much shorter `NOMINATIM_MISS_TTL_S`, because "no district is mapped
     here" is a statement about OSM's coverage today and coverage improves.
  2. **Coalescing.** Concurrent callers asking the identical question elect one
     leader to make the request; the rest wait for its answer. A city going
     viral costs one upstream call, not one per request.
  3. **Rate limiting.** What is left reaches `_throttle`, which hands out slots
     no faster than the policy allows.

Transient failures are never cached as misses. A 502 or a dropped connection
does get a *very* short error marker so that one outage does not turn into a
queue of callers each burning a rate-limit slot to rediscover it, but that
marker is measured in seconds and is deliberately not the same thing as a
cached "not found".
"""
import logging
import math
import time

import requests
from django.conf import settings
from django.db import IntegrityError

from . import geometry, upstream
from .models import Region

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────
# Defaults, all overridable as NOMINATIM_<NAME> in settings. Read through
# `_conf` at call time rather than bound at import, so a deployment — or a test
# — can change them without reloading the module.

# Nominatim's policy is 1 req/s. The extra 100ms is slack for clock jitter
# between workers — being slightly under the limit costs nothing, being over it
# risks the whole application being blocked. Set to 0 to disable throttling
# entirely, which is only sane against an instance you run yourself.
DEFAULT_MIN_INTERVAL_S = 1.1
# How many requests may be in flight at once. One, for the public instance.
DEFAULT_MAX_CONCURRENCY = 1
# How long a caller will queue for a slot. Separate from the network timeout
# below on purpose: a request that spent four seconds waiting its turn must
# still get a full network budget, or a queue and a slow upstream compound into
# a deadline nobody can meet.
DEFAULT_MAX_QUEUE_WAIT_S = 5.0
# Give up rather than hold a request open: a slow upstream must not become a
# slow API for the person waiting on the picker.
DEFAULT_TIMEOUT_S = 8.0
# A border does not move. The only reason this expires at all is so a place
# remapped in OSM eventually reaches us.
DEFAULT_CACHE_TTL_S = 7 * 24 * 60 * 60
# "Nothing is mapped here" is a fact about OSM's coverage, and coverage grows.
DEFAULT_MISS_TTL_S = 10 * 60
# Long enough to stop an outage becoming a stampede, short enough that it is
# not a cached answer in any meaningful sense.
DEFAULT_ERROR_TTL_S = 3.0
# How long a follower waits for the leader's answer before giving up on
# coalescing. Covers the worst case the leader itself faces: a full queue wait
# plus a full network timeout.
DEFAULT_COALESCE_WAIT_S = DEFAULT_MAX_QUEUE_WAIT_S + DEFAULT_TIMEOUT_S

POLL_INTERVAL_S = upstream.POLL_INTERVAL_S

SERVICE = 'nominatim'


def _conf(name, default):
    return getattr(settings, f'NOMINATIM_{name}', default)


def _ttls():
    return {
        'hit': _conf('CACHE_TTL_S', DEFAULT_CACHE_TTL_S),
        'miss': _conf('MISS_TTL_S', DEFAULT_MISS_TTL_S),
        'error': _conf('ERROR_TTL_S', DEFAULT_ERROR_TTL_S),
        'coalesce_wait': _conf('COALESCE_WAIT_S', DEFAULT_COALESCE_WAIT_S),
    }


# A city boundary from OSM routinely carries 20k+ points, which is more than a
# phone should download or a map should draw.
#
# ~0.0002 degrees is roughly 22m. Chosen against the MAX_POINTS budget below
# rather than for its own sake: a large city lands around a thousand points at
# this tolerance, comfortably inside the cap, so there is no reason to blur
# further. A coarser 0.0005 collapsed a 460-point city district to 18 -- fine
# from orbit, visibly polygonal once you zoom to the streets it follows.
SIMPLIFY_TOLERANCE_DEG = 0.0002
# Hard ceiling regardless of tolerance, so one pathological coastline cannot
# produce a payload that stalls the map. Tolerance is doubled until it fits.
MAX_POINTS = 1500

# Only real administrative areas. Without this a search for a city name happily
# returns a bus stop or a shop with the same name, which cannot bound a zone.
#
# `landuse` is *not* here, which is why there is no `residential` among the
# district types below: a residential land-use polygon is a planning artefact,
# not a place anyone calls their district, and it never reaches classification.
ALLOWED_CLASSES = {'boundary', 'place'}

# Nominatim's `addresstype` for the two sizes a zone offers. The lists are
# generous because the same word means different things by country: a French
# "commune" and a US "city" are both what someone means by "my city", and
# "borough" is a whole city in one country and a slice of one in another --
# which is why containment and the address hierarchy break the tie below rather
# than the word alone.
CITY_TYPES = {
    'city', 'town', 'village', 'municipality', 'hamlet', 'commune',
}
DISTRICT_TYPES = {
    'suburb', 'neighbourhood', 'quarter', 'city_district', 'district',
    'borough', 'city_block', 'subdistrict', 'ward',
}

# Values that carry no size information on their own. `administrative` is the
# common one: Nominatim answers with it for a boundary relation it could not
# place at a named level in the address hierarchy, and it covers everything
# from a parish to a country. Nothing in this set is ever accepted as a size on
# its own — only when `_hint_applies` finds independent corroboration.
INCONCLUSIVE_TYPES = {'administrative', 'boundary', 'yes', ''}

# One fine-grained reverse call, used only to read the *address hierarchy* out
# of the response — never the object it resolves to.
#
# Choosing a level by zoom does not work. Reverse zoom maps onto OSM's
# admin_level, and admin_level means different things by country: in Almaty the
# city is level 4 and its districts are level 6, so zoom 10 -- nominally "city"
# -- returns a district. Go finer and it returns a `place=suburb` *node*, a
# point with no boundary at all, which cannot bound a zone.
#
# The `address` object, by contrast, names every level explicitly and
# consistently, which is what the two buttons actually need.
ADDRESS_ZOOM = 14

# Address keys per level, most specific first — the first key present wins.
# Nominatim uses whichever the country's data provides, so all of them have to
# be tried.
#
# `ward` is last deliberately. It is the primary city subdivision in Tanzania,
# Kenya, Vietnam and much of Japan, so it has to be here at all; but in the UK
# a ward is a few thousand people inside a borough, and where both exist the
# borough is what somebody means by "my district".
ADDRESS_KEYS = {
    'city': ('city', 'town', 'village', 'municipality'),
    'district': (
        'city_district', 'district', 'borough', 'suburb', 'quarter',
        'neighbourhood', 'ward',
    ),
}


class NominatimError(upstream.UpstreamError):
    """Upstream was unreachable, slow, or returned something unusable."""


class NominatimBusy(NominatimError, upstream.UpstreamBusy):
    """No capacity right now — the caller should try again shortly.

    Distinct from its parent because it is the one failure that is purely about
    load rather than about the upstream being broken, so it deserves a
    Retry-After rather than a bare "try again later". Inheriting from both sides
    is what lets `upstream.fetch` recognise it as capacity — and so decline to
    cache it — while every caller here still catches it as a NominatimError.
    """


# ─── Structured logging ───────────────────────────────────────────────────────
def _event(event, **fields):
    """One structured line per notable step of a lookup.

    Deliberately flat and greppable: `nominatim cache_hit path=search ...`.
    The same fields go out under a single `nominatim` key in `extra`, so a
    JSON formatter emits them as structure rather than as a prose message.
    """
    # Classification categories help diagnose gaps; queried places/OSM IDs
    # identify a person's location and do not belong in request-correlated logs.
    safe = {'reason', 'osm_type', 'klass', 'type', 'addresstype', 'admin_level',
            'country_code', 'address_keys', 'wanted', 'got', 'kind'}
    present = {k: v for k, v in fields.items() if k in safe and v is not None}
    logger.info(
        'nominatim %s %s',
        event,
        ' '.join(f'{k}={v}' for k, v in present.items()),
        extra={'nominatim': {'event': event, **present}},
    )


def _log_rejected(reason, payload, **extra):
    """Record a Nominatim result we declined to use, and everything needed to
    tell *why* from the logs alone.

    This exists because the district lookup's real failure mode is invisible
    from the outside: someone in Dar es Salaam taps "Use my district", gets
    "nothing mapped", and the only thing distinguishing "OSM has no polygon"
    from "we do not recognise this country's word for a district" is the
    `addresstype` on the result we threw away. Aggregating this event by
    `addresstype` is how the type lists above get extended with evidence rather
    than guesswork.
    """
    address = payload.get('address') or {}
    _event(
        'rejected',
        reason=reason,
        osm_type=payload.get('osm_type'),
        osm_id=payload.get('osm_id'),
        # `class` is a reserved word in the payload's own vocabulary, not ours.
        klass=payload.get('class') or payload.get('category'),
        type=payload.get('type'),
        addresstype=payload.get('addresstype'),
        admin_level=(payload.get('extratags') or {}).get('admin_level'),
        name=payload.get('name'),
        display_name=payload.get('display_name'),
        country_code=address.get('country_code'),
        # Only the levels the two buttons can ever resolve to, so the line stays
        # readable; the full hierarchy is rarely what is missing.
        address_keys=','.join(
            k for k in (*ADDRESS_KEYS['city'], *ADDRESS_KEYS['district'])
            if address.get(k)
        ) or None,
        **extra,
    )


# ─── Rate limiting ────────────────────────────────────────────────────────────
# Process-local fallback deadline, used only when the shared cache is
# unreachable. A one-element list rather than a float so `upstream.claim_slot`
# can advance it without this module having to hand it a setter.
_local_clock = [0.0]


def _claim_slot():
    """Try to take a request slot. True when one is ours.

    With `NOMINATIM_MAX_CONCURRENCY` above 1 there are that many slots and a
    caller takes the first free one — the same election, widened, for an
    instance whose policy allows parallel requests. See regions.upstream for
    why it is built on `cache.add` and how it degrades.
    """
    return upstream.claim_slot(
        SERVICE,
        _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S),
        _conf('MAX_CONCURRENCY', DEFAULT_MAX_CONCURRENCY),
        _local_clock,
    )


def _throttle():
    """Block until a request is allowed. Returns the seconds spent waiting.

    The wait has its own budget, `NOMINATIM_MAX_QUEUE_WAIT_S`, which is not the
    network timeout: queueing behind other callers and a slow upstream are
    different problems and must not share one deadline. Exhausting the queue
    budget raises `NominatimBusy`, which the views turn into a retryable answer
    rather than a flat failure.
    """
    if _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S) <= 0:
        return 0.0

    started = time.monotonic()
    budget = _conf('MAX_QUEUE_WAIT_S', DEFAULT_MAX_QUEUE_WAIT_S)
    deadline = started + budget
    while True:
        if _claim_slot():
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            raise NominatimBusy(
                f'No boundary-service capacity after {budget:.0f}s.',
                retry_after=max(1, math.ceil(
                    _conf('MIN_INTERVAL_S', DEFAULT_MIN_INTERVAL_S)
                )),
            )
        time.sleep(POLL_INTERVAL_S)


# ─── Transport ────────────────────────────────────────────────────────────────
def _get(path, params):
    """One upstream request. No caching and no coalescing — see `_fetch`.

    Every call that reaches here is a real HTTP request against Nominatim, so
    this is the seam tests patch to exercise everything above it without a
    network.
    """
    queue_wait = _throttle()
    url = f'{settings.NOMINATIM_URL.rstrip("/")}/{path}'
    timeout = _conf('TIMEOUT_S', DEFAULT_TIMEOUT_S)
    started = time.monotonic()

    def elapsed_ms():
        return round((time.monotonic() - started) * 1000)

    try:
        response = requests.get(
            url,
            params=params,
            headers={
                'User-Agent': settings.NOMINATIM_USER_AGENT,
                'Accept': 'application/json',
            },
            timeout=timeout,
        )
    except requests.Timeout as exc:
        upstream.event(
            SERVICE, 'upstream_timeout', path=path,
            queue_wait_ms=round(queue_wait * 1000), upstream_ms=elapsed_ms(),
        )
        raise NominatimError(f'Boundary service timed out after {timeout}s.') from exc
    except requests.RequestException as exc:
        upstream.event(
            SERVICE, 'upstream_error', path=path, error=type(exc).__name__,
            queue_wait_ms=round(queue_wait * 1000), upstream_ms=elapsed_ms(),
        )
        raise NominatimError(f'Could not reach the boundary service: {exc}') from exc

    if response.status_code != 200:
        upstream.event(
            SERVICE, 'upstream_status', path=path, status=response.status_code,
            queue_wait_ms=round(queue_wait * 1000), upstream_ms=elapsed_ms(),
        )
        raise NominatimError(f'Boundary service returned {response.status_code}.')
    try:
        payload = response.json()
    except ValueError as exc:
        upstream.event(SERVICE, 'upstream_unparseable', path=path, upstream_ms=elapsed_ms())
        raise NominatimError('Boundary service returned a non-JSON body.') from exc

    upstream.event(
        SERVICE, 'upstream_ok', path=path,
        queue_wait_ms=round(queue_wait * 1000), upstream_ms=elapsed_ms(),
    )
    return payload


# ─── Cache and coalescing ─────────────────────────────────────────────────────
def _cache_key(path, params):
    """The response-cache key for one exact question."""
    return upstream.cache_key(SERVICE, path, params)


def _fetch(path, params):
    """A Nominatim answer, from cache, from a concurrent caller, or upstream.

    This is what `search`, `reverse` and `lookup` call. `_get` stays underneath
    it as the bare transport, so the rate limiter guards real requests only and
    a test that patches `_get` still exercises everything above it.
    """
    return upstream.fetch(SERVICE, path, params, lambda: _get(path, params), _ttls())


# ─── Geometry ─────────────────────────────────────────────────────────────────
def _perpendicular_distance(point, start, end):
    """Distance from `point` to the segment start–end, in degrees.

    Degrees, not metres, deliberately: this only ever compares one distance
    against another within a single small polygon, so the latitude distortion
    is identical for every candidate and cancels out. Converting would cost a
    trig call per point across tens of thousands of points and change nothing.
    """
    x, y = point[0], point[1]
    x1, y1 = start[0], start[1]
    x2, y2 = end[0], end[1]

    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5

    # Area of the parallelogram over the base length.
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / ((dx * dx + dy * dy) ** 0.5)


def simplify(points, tolerance):
    """Ramer–Douglas–Peucker, iterative.

    Iterative rather than recursive on purpose: a 20,000-point coastline
    recurses deep enough to blow Python's stack, and this runs inside a request.

    Expects longitudes already unwrapped by `geometry.unwrap_rings`. Run over
    raw GeoJSON that crosses the dateline, the +179 → -179 step reads as an
    enormous excursion and the algorithm preserves exactly the vertices it
    should be dropping.
    """
    if len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst_dist, worst_idx = 0.0, first
        for i in range(first + 1, last):
            d = _perpendicular_distance(points[i], points[first], points[last])
            if d > worst_dist:
                worst_dist, worst_idx = d, i
        if worst_dist > tolerance:
            keep[worst_idx] = True
            stack.append((first, worst_idx))
            stack.append((worst_idx, last))

    return [p for p, k in zip(points, keep) if k]


def _rings_from_geojson(geometry_obj):
    """Flatten a GeoJSON geometry to a list of [lon, lat] rings.

    Nominatim answers with Polygon or MultiPolygon depending on whether the
    place is one landmass or several, and a city with an island or an exclave
    is a MultiPolygon. Both collapse to the same flat list of rings here,
    because the containment test treats them identically.
    """
    if not isinstance(geometry_obj, dict):
        return []
    kind = geometry_obj.get('type')
    coords = geometry_obj.get('coordinates') or []

    if kind == 'Polygon':
        return [ring for ring in coords if len(ring) >= 4]
    if kind == 'MultiPolygon':
        return [ring for polygon in coords for ring in polygon if len(ring) >= 4]
    return []


def _simplify_rings(rings):
    """Simplify every ring, backing off until the whole set fits MAX_POINTS."""
    tolerance = SIMPLIFY_TOLERANCE_DEG
    for _ in range(8):
        reduced = []
        for ring in rings:
            simple = simplify(ring, tolerance)
            # A ring needs 3 distinct corners to enclose anything; anything less
            # has been simplified out of existence and would only add noise.
            if len(simple) >= 4:
                reduced.append([[float(p[0]), float(p[1])] for p in simple])
        total = sum(len(r) for r in reduced)
        if reduced and total <= MAX_POINTS:
            return reduced, total
        tolerance *= 2
    return reduced, sum(len(r) for r in reduced)


# ─── Upsert ───────────────────────────────────────────────────────────────────
def _bbox(payload, rings):
    """Prefer the upstream's own bounding box; fall back to the geometry.

    Nominatim's `boundingbox` is [min_lat, max_lat, min_lon, max_lon] — note
    the interleaving, which is not the order anything else uses.

    A region crossing the antimeridian is the one case where the upstream box
    is worse than useless: Nominatim reports it wrapped, as [-180, 180], which
    is a box around the entire planet and rejects nothing. For those the
    unwrapped geometry is the only honest source — see regions.geometry.
    """
    unwrapped_bounds = geometry.bounds(rings) if rings else None
    if unwrapped_bounds and geometry.crosses_antimeridian(
        unwrapped_bounds[1], unwrapped_bounds[3]
    ):
        return unwrapped_bounds

    box = payload.get('boundingbox')
    if box and len(box) == 4:
        try:
            return float(box[0]), float(box[2]), float(box[1]), float(box[3])
        except (TypeError, ValueError):
            pass

    if not unwrapped_bounds:
        raise NominatimError('Place has no usable boundary.')
    return unwrapped_bounds


def _region_from_payload(payload):
    """Turn one Nominatim result into a saved Region, or None if unusable.

    Returns the existing row when we already hold this OSM object, so a second
    person picking the same city never causes a second fetch or a duplicate.
    """
    osm_type = (payload.get('osm_type') or '').lower()
    osm_id = payload.get('osm_id')
    if osm_type not in Region.OSMType.values or not osm_id:
        _log_rejected('not-an-osm-object', payload)
        return None

    existing = Region.objects.filter(osm_type=osm_type, osm_id=osm_id).first()
    if existing:
        # Re-classify from the fresh payload. A cached row can carry a wrong
        # size -- it was classified by an older rule, or by a migration working
        # from less information than this payload has -- and a short-circuit
        # that trusts it makes that error permanent: the cache satisfies the
        # lookup, so the upstream answer that would correct it is never seen.
        kind = _classify(payload)
        if kind != Region.Kind.OTHER and kind != existing.kind:
            existing.kind = kind
            existing.save(update_fields=['kind'])
        return existing

    rings = _rings_from_geojson(payload.get('geojson'))
    if not rings:
        # A place Nominatim only knows as a point — a village marker with no
        # mapped border. It cannot bound a zone, so it is not offered.
        _log_rejected('no-mapped-boundary', payload)
        return None

    # Unwrap before simplifying and before measuring. Both steps read longitude
    # as a plain number, and across the dateline the raw values are not one.
    rings = geometry.unwrap_rings(rings)
    if not rings:
        _log_rejected('no-mapped-boundary', payload)
        return None

    rings, total = _simplify_rings(rings)
    if not rings:
        _log_rejected('boundary-simplified-away', payload)
        return None

    min_lat, min_lon, max_lat, max_lon = _bbox(payload, rings)
    centroid_lat, centroid_lon = geometry.centroid(min_lat, min_lon, max_lat, max_lon)
    address = payload.get('address') or {}
    kind = _classify(payload)
    if kind == Region.Kind.OTHER:
        _log_rejected('unclassifiable-addresstype', payload)

    region = Region(
        osm_type=osm_type,
        osm_id=osm_id,
        name=(payload.get('name') or payload.get('display_name') or '')[:200],
        display_name=(payload.get('display_name') or '')[:500],
        country_code=(address.get('country_code') or '')[:2],
        kind=kind,
        admin_level=_admin_level(payload),
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        rings=rings,
        point_count=total,
    )
    try:
        region.save()
    except IntegrityError:
        # Another request cached the same place between the check above and
        # here. Theirs is as good as ours.
        return Region.objects.filter(osm_type=osm_type, osm_id=osm_id).first()
    return region


def _addresstype(payload):
    return (payload.get('addresstype') or payload.get('type') or '').lower()


def _classify(payload):
    """Which of the two zone sizes this place is.

    `addresstype` and nothing else. There used to be an admin_level fallback --
    6-8 city, 9+ district -- and it is precisely what broke this feature in the
    field: Almaty's *districts* are admin_level 6, so the fallback labelled a
    district a city, and the mislabelled row then satisfied every city lookup
    from cache. Guessing a level's meaning from its number is not possible;
    the number means different things in different countries.

    'other' is an honest answer. A province or a country is neither of the two
    things the buttons offer, and saying so beats a confident wrong label. It
    is also not final: `_resolve_boundary` can promote an 'other' to a real
    size when the address hierarchy corroborates one — but only there, where
    the point and the name are both known.
    """
    addresstype = _addresstype(payload)
    if addresstype in CITY_TYPES:
        return Region.Kind.CITY
    if addresstype in DISTRICT_TYPES:
        return Region.Kind.DISTRICT
    return Region.Kind.OTHER


def _admin_level(payload):
    raw = (payload.get('extratags') or {}).get('admin_level')
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return None
    return level if 0 < level < 32 else None


def _is_area(payload):
    return (payload.get('class') or payload.get('category')) in ALLOWED_CLASSES


def _same_name(left, right):
    return bool(left) and bool(right) and left.strip().casefold() == right.strip().casefold()


def _hint_applies(payload, name_hint):
    """Whether an inconclusive result may be trusted as the size we asked for.

    The corroboration is provenance, not the payload's own label. `name_hint`
    is a name Nominatim itself placed at the requested level of the address
    hierarchy *for the point we are standing on*; the caller has already
    confirmed this boundary contains that point. So when an `administrative`
    relation comes back carrying exactly that name, three independent facts
    agree on what it is, and none of them is a guess about admin_level.

    A country or a state cannot reach here. Their `addresstype` says what they
    are, which keeps them out of `INCONCLUSIVE_TYPES` entirely — the hint only
    ever fills a genuine blank, never overrides an answer.
    """
    if not name_hint:
        return False
    if _addresstype(payload) not in INCONCLUSIVE_TYPES:
        return False
    return _same_name(payload.get('name'), name_hint)


# ─── Public API ───────────────────────────────────────────────────────────────
def search(query, limit=8):
    """Places matching `query` that have a real mapped boundary."""
    payloads = _fetch('search', {
        'q': query,
        'format': 'jsonv2',
        'polygon_geojson': 1,
        'addressdetails': 1,
        'extratags': 1,
        'limit': limit,
        'dedupe': 1,
    })
    if not isinstance(payloads, list):
        raise NominatimError('Unexpected search response.')

    regions = []
    for payload in payloads:
        if not _is_area(payload):
            _log_rejected('class-is-not-an-area', payload, query=query)
            continue
        region = _region_from_payload(payload)
        if region:
            regions.append(region)
    return regions


def lookup(osm_refs):
    """Boundaries for specific OSM objects, by reference.

    The counterpart to `search`, and the reason the Overpass fallback is cheap
    to bolt on: Overpass is very good at answering "which boundary relations
    contain this point" and produces geometry as a bag of unordered ways, which
    would need assembling into rings. Nominatim's /lookup hands back the same
    clean GeoJSON as every other call here for objects it already indexes. So
    Overpass decides *which*, and this fetches *what*.

    `osm_refs` are Nominatim's own form — 'R1234', 'W56', 'N78'. It accepts up
    to 50 per call; more than a handful never arises from one point.
    """
    refs = [ref for ref in osm_refs if ref]
    if not refs:
        return []

    payloads = _fetch('lookup', {
        'osm_ids': ','.join(refs[:50]),
        'format': 'jsonv2',
        'polygon_geojson': 1,
        'addressdetails': 1,
        'extratags': 1,
    })
    if not isinstance(payloads, list):
        raise NominatimError('Unexpected lookup response.')

    regions = []
    for payload in payloads:
        if not _is_area(payload):
            _log_rejected('class-is-not-an-area', payload, source='lookup')
            continue
        region = _region_from_payload(payload)
        if region:
            regions.append(region)
    return regions


def address_at(lat, lon):
    """The raw address hierarchy at a point, or None if there is not one.

    Shares its cached response with `reverse` — identical parameters, so the
    second of the two costs nothing. That matters because the resolver wants
    the whole hierarchy while `reverse` wants one level of it, and neither
    should pay for the other.
    """
    payload = _fetch('reverse', {
        'lat': lat,
        'lon': lon,
        'format': 'jsonv2',
        'addressdetails': 1,
        'extratags': 1,
        'zoom': ADDRESS_ZOOM,
    })
    if not isinstance(payload, dict) or payload.get('error'):
        return None
    return payload.get('address') or {}


def _address_name(address, kind):
    for key in ADDRESS_KEYS.get(kind, ()):
        value = address.get(key)
        if value:
            return value
    return None


def _resolve_boundary(query, country_code, kind, lat, lon, name_hint=None):
    """Find the boundary named `query` that actually contains the point.

    Searching by name is what makes this reliable across countries — but a
    name alone is ambiguous, and there are a dozen places called Almaty. The
    containment check is what disambiguates: only one of them can contain the
    coordinates we started from. That is also why the kind is re-checked here
    rather than trusted from the address key that produced the name.
    """
    params = {
        'q': query,
        'format': 'jsonv2',
        'polygon_geojson': 1,
        'addressdetails': 1,
        'extratags': 1,
        'limit': 8,
    }
    if country_code:
        params['countrycodes'] = country_code

    for payload in _fetch('search', params) or []:
        if not _is_area(payload):
            _log_rejected('class-is-not-an-area', payload, query=query, wanted=kind)
            continue
        region = _region_from_payload(payload)
        # Everything fetched is worth keeping as cache even when it is not the
        # answer — but only a containing shape of the right size is the answer.
        if not region:
            continue
        if not region.contains(lat, lon):
            _log_rejected(
                'does-not-contain-the-point', payload,
                query=query, wanted=kind, lat=lat, lon=lon,
            )
            continue
        if region.kind == kind:
            return region
        # Right place, no usable label. Promote it only now — after containment
        # — so a hint can never write a size onto a row somebody else's lookup
        # will read. That ordering is the lesson from the admin_level bug: a
        # wrong kind in this table is permanent, because the cache answers
        # before upstream ever gets asked again.
        if region.kind == Region.Kind.OTHER and _hint_applies(payload, name_hint):
            region.kind = kind
            region.save(update_fields=['kind'])
            _event(
                'classified_by_address_hint',
                osm_type=region.osm_type, osm_id=region.osm_id,
                addresstype=_addresstype(payload), name=region.name, kind=kind,
            )
            return region
        _log_rejected(
            'wrong-size-for-this-lookup', payload,
            query=query, wanted=kind, got=region.kind,
        )
    return None

