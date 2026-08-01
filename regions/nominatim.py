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
require touching this code.
"""
import logging
import time
from typing import NamedTuple

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError

from .models import Region

logger = logging.getLogger(__name__)

# Nominatim's policy is 1 req/s. The extra 100ms is slack for clock jitter
# between workers — being slightly under the limit costs nothing, being over it
# risks the whole application being blocked.
MIN_INTERVAL_S = 1.1
_THROTTLE_KEY = 'regions:nominatim:next-allowed'

# Give up rather than hold a request open: a slow upstream must not become a
# slow API for the person waiting on the picker.
TIMEOUT_S = 8

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
ALLOWED_CLASSES = {'boundary', 'place'}

# Nominatim's `addresstype` for the two sizes a zone offers. The lists are
# generous because the same word means different things by country: a French
# "commune" and a US "city" are both what someone means by "my city", and
# "borough" is a whole city in one country and a slice of one in another --
# which is why admin_level breaks the tie below rather than the word alone.
CITY_TYPES = {
    'city', 'town', 'village', 'municipality', 'hamlet', 'commune',
}
DISTRICT_TYPES = {
    'suburb', 'neighbourhood', 'quarter', 'city_district', 'district',
    'borough', 'city_block', 'residential', 'subdistrict',
}

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

# Address keys per level, most specific first. Nominatim uses whichever the
# country's data provides, so all of them have to be tried.
ADDRESS_KEYS = {
    'city': ('city', 'town', 'village', 'municipality'),
    'district': (
        'city_district', 'district', 'borough', 'suburb', 'quarter',
        'neighbourhood',
    ),
}


class NominatimError(Exception):
    """Upstream was unreachable, slow, or returned something unusable."""


# ─── Rate limiting ────────────────────────────────────────────────────────────
# Process-local fallback, used only when the shared cache is unreachable. Less
# strict than the real thing — each worker would keep its own clock — but the
# alternative is either a 500 or an unthrottled call, and both are worse.
_local_next_allowed = 0.0


def _claim_slot():
    """Try to take the next request slot. True when it is ours.

    `cache.add` is atomic, which is the whole point: whoever adds the key wins
    and proceeds, everyone else waits. A plain get/set would let two workers
    read the same timestamp and both fire.

    A cache that raises — Redis restarting, credentials rotated — must not turn
    a place search into a 500. Falling back keeps the limit approximately held
    rather than abandoning it.
    """
    global _local_next_allowed
    try:
        return bool(cache.add(_THROTTLE_KEY, '1', timeout=int(MIN_INTERVAL_S) + 1))
    except Exception:
        logger.warning('Rate-limit cache unavailable; falling back to a local clock.')
        now = time.monotonic()
        if now < _local_next_allowed:
            return False
        _local_next_allowed = now + MIN_INTERVAL_S
        return True


def _throttle():
    """Block until the next request is allowed."""
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        if _claim_slot():
            return
        if time.monotonic() > deadline:
            raise NominatimError('Timed out waiting for a rate-limit slot.')
        time.sleep(0.15)


def _get(path, params):
    _throttle()
    url = f'{settings.NOMINATIM_URL.rstrip("/")}/{path}'
    try:
        response = requests.get(
            url,
            params=params,
            headers={
                'User-Agent': settings.NOMINATIM_USER_AGENT,
                'Accept': 'application/json',
            },
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise NominatimError(f'Could not reach the boundary service: {exc}') from exc

    if response.status_code != 200:
        raise NominatimError(f'Boundary service returned {response.status_code}.')
    try:
        return response.json()
    except ValueError as exc:
        raise NominatimError('Boundary service returned a non-JSON body.') from exc


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


def _rings_from_geojson(geometry):
    """Flatten a GeoJSON geometry to a list of [lon, lat] rings.

    Nominatim answers with Polygon or MultiPolygon depending on whether the
    place is one landmass or several, and a city with an island or an exclave
    is a MultiPolygon. Both collapse to the same flat list of rings here,
    because the containment test treats them identically.
    """
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get('type')
    coords = geometry.get('coordinates') or []

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
    """
    box = payload.get('boundingbox')
    if box and len(box) == 4:
        try:
            return float(box[0]), float(box[2]), float(box[1]), float(box[3])
        except (TypeError, ValueError):
            pass

    lats = [p[1] for ring in rings for p in ring]
    lons = [p[0] for ring in rings for p in ring]
    if not lats:
        raise NominatimError('Place has no usable boundary.')
    return min(lats), min(lons), max(lats), max(lons)


def _region_from_payload(payload):
    """Turn one Nominatim result into a saved Region, or None if unusable.

    Returns the existing row when we already hold this OSM object, so a second
    person picking the same city never causes a second fetch or a duplicate.
    """
    osm_type = (payload.get('osm_type') or '').lower()
    osm_id = payload.get('osm_id')
    if osm_type not in Region.OSMType.values or not osm_id:
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
        return None

    rings, total = _simplify_rings(rings)
    if not rings:
        return None

    min_lat, min_lon, max_lat, max_lon = _bbox(payload, rings)
    address = payload.get('address') or {}

    region = Region(
        osm_type=osm_type,
        osm_id=osm_id,
        name=(payload.get('name') or payload.get('display_name') or '')[:200],
        display_name=(payload.get('display_name') or '')[:500],
        country_code=(address.get('country_code') or '')[:2],
        kind=_classify(payload),
        admin_level=_admin_level(payload),
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=(min_lat + max_lat) / 2,
        centroid_lon=(min_lon + max_lon) / 2,
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


def _classify(payload):
    """Which of the two zone sizes this place is.

    `addresstype` and nothing else. There used to be an admin_level fallback --
    6-8 city, 9+ district -- and it is precisely what broke this feature in the
    field: Almaty's *districts* are admin_level 6, so the fallback labelled a
    district a city, and the mislabelled row then satisfied every city lookup
    from cache. Guessing a level's meaning from its number is not possible;
    the number means different things in different countries.

    'other' is an honest answer. A province or a country is neither of the two
    things the buttons offer, and saying so beats a confident wrong label.
    """
    addresstype = (payload.get('addresstype') or payload.get('type') or '').lower()
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


# ─── Public API ───────────────────────────────────────────────────────────────
def search(query, limit=8):
    """Places matching `query` that have a real mapped boundary."""
    payloads = _get('search', {
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
            continue
        region = _region_from_payload(payload)
        if region:
            regions.append(region)
    return regions


class ReverseResult(NamedTuple):
    """What a "where am I" lookup could establish.

    `region` is the boundary at the level asked for, when one exists. `name`
    and `city_name` come from the address hierarchy and survive even when no
    polygon could be resolved — enough for the caller to say *which* place it
    failed to find, which is far more useful than a bare miss.
    """
    region: object | None
    name: str | None
    city_name: str | None


def _address_name(address, kind):
    for key in ADDRESS_KEYS.get(kind, ()):
        value = address.get(key)
        if value:
            return value
    return None


def _resolve_boundary(query, country_code, kind, lat, lon):
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

    for payload in _get('search', params) or []:
        if not _is_area(payload):
            continue
        region = _region_from_payload(payload)
        # Everything fetched is worth keeping as cache even when it is not the
        # answer — but only a containing shape of the right size is the answer.
        if region and region.kind == kind and region.contains(lat, lon):
            return region
    return None


def reverse(lat, lon, kind=Region.Kind.CITY):
    """The boundary of a given size containing a point.

    Two upstream calls on a miss: one reverse to learn what the place is
    called at each level, one search to fetch that level's actual boundary.
    Both are cached afterwards, so the second person standing in the same
    district costs nothing.
    """
    payload = _get('reverse', {
        'lat': lat,
        'lon': lon,
        'format': 'jsonv2',
        'addressdetails': 1,
        'extratags': 1,
        'zoom': ADDRESS_ZOOM,
    })
    if not isinstance(payload, dict) or payload.get('error'):
        return ReverseResult(None, None, None)

    address = payload.get('address') or {}
    country_code = (address.get('country_code') or '')[:2]
    city_name = _address_name(address, Region.Kind.CITY)
    name = _address_name(address, kind)
    if not name:
        return ReverseResult(None, None, city_name)

    # A district is qualified by its city: "Almaly" alone is ambiguous in a way
    # that "Almaly, Almaty" is not, and it narrows the search enough that the
    # containing candidate is usually the first result.
    query = name
    if kind == Region.Kind.DISTRICT and city_name and city_name != name:
        query = f'{name}, {city_name}'

    region = _resolve_boundary(query, country_code, kind, lat, lon)
    return ReverseResult(region, name, city_name)
