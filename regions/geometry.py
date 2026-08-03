"""Antimeridian-safe geometry for region boundaries.

Every coordinate here is WGS-84. That is the canonical system for stored
geometry and for every containment decision, and nothing in this module
changes it — what it changes is how a longitude is *represented*.

A polygon crossing the ±180° meridian — Fiji, Kiribati, Chukotka, the Chatham
Islands — arrives from OpenStreetMap with longitudes that jump from +179.9 to
-179.9 between two adjacent points. Read naively that is not a short hop over
the dateline, it is a 359.8° sweep back around the planet, and it poisons
everything downstream: the bounding box widens to [-180, 180] and stops
rejecting anything, and the ray cast walks edges spanning half the globe.

The fix is to *unwrap*. Walk the ring and, whenever a step would exceed half a
turn, add or subtract whole turns so the path stays continuous. Fiji then
reads as roughly [178.2, 180.4] instead of [-180, 180]. Longitudes past ±180
are legal in this representation, and are precisely the marker that a region
crosses at all.

Query points still arrive in the ordinary ±180 range, so `align_lon` moves a
query into whichever window its region is expressed in before any comparison.

For regions that do not cross — every region that existed before this module —
unwrapping is the identity, and the stored numbers are unchanged.
"""
import math

TURN = 360.0
HALF_TURN = 180.0


# ─── Longitude windows ────────────────────────────────────────────────────────
def wrap_lon(lon):
    """Bring a longitude back into the ordinary ±180 range.

    Used only for values handed to a map for display — a centroid, a viewport.
    Never for stored ring geometry, which stays unwrapped so it remains
    continuous.
    """
    wrapped = math.fmod(lon + HALF_TURN, TURN)
    if wrapped < 0:
        wrapped += TURN
    return wrapped - HALF_TURN


def align_lon(lon, min_lon, max_lon):
    """Express `lon` in the same window as a region spanning min_lon..max_lon.

    Chooses the whole-turn shift that lands nearest the middle of the window,
    which is the representation that can actually fall inside it. For a region
    that does not cross, the shift is zero and this returns `lon` untouched.
    """
    middle = (min_lon + max_lon) / 2
    shift = round((middle - lon) / TURN)
    return lon + shift * TURN if shift else lon


def crosses_antimeridian(min_lon, max_lon):
    """Whether a stored window runs past ±180, i.e. over the dateline."""
    return max_lon > HALF_TURN or min_lon < -HALF_TURN


# ─── Unwrapping ───────────────────────────────────────────────────────────────
def unwrap_ring(ring):
    """One ring with continuous longitudes, as [lon, lat] floats.

    Each point is moved by whole turns to sit within half a turn of the point
    before it, which is the only interpretation that keeps consecutive vertices
    neighbours rather than antipodes.
    """
    points = [p for p in ring if p is not None and len(p) >= 2]
    if not points:
        return []

    out = [[float(points[0][0]), float(points[0][1])]]
    for point in points[1:]:
        lon, lat = float(point[0]), float(point[1])
        previous = out[-1][0]
        # Whole turns, computed rather than looped: a ring winding around a
        # pole would otherwise iterate once per turn.
        shift = round((previous - lon) / TURN)
        out.append([lon + shift * TURN if shift else lon, lat])
    return out


def _mean_lon(ring):
    return sum(p[0] for p in ring) / len(ring)


def _recentre(rings):
    """Slide every ring by whole turns until the set starts at or after -180.

    Canonical form, so two fetches of the same place cannot be stored in two
    different windows. A region that does not cross already satisfies this and
    is returned untouched.
    """
    min_lon = min(p[0] for ring in rings for p in ring)
    if min_lon >= -HALF_TURN:
        return rings
    shift = math.ceil((-HALF_TURN - min_lon) / TURN) * TURN
    return [[[lon + shift, lat] for lon, lat in ring] for ring in rings]


def unwrap_rings(rings):
    """Unwrap every ring, then bring them all into one shared window.

    Rings are unwrapped independently, so a multipolygon can come out with one
    island near +179 and another near -179 — the same neighbourhood, described
    two ways. They are only comparable once they share a window, so each ring
    is shifted by whole turns to sit nearest the first.

    Fiji is the reason: its main landmass and Taveuni sit either side of the
    dateline, and a bounding box taken across two windows spans the planet.
    """
    unwrapped = [r for r in (unwrap_ring(ring) for ring in rings) if r]
    if not unwrapped:
        return []

    anchor = _mean_lon(unwrapped[0])
    aligned = [unwrapped[0]]
    for ring in unwrapped[1:]:
        shift = round((anchor - _mean_lon(ring)) / TURN)
        aligned.append(
            [[lon + shift * TURN, lat] for lon, lat in ring] if shift else ring
        )
    return _recentre(aligned)


# ─── Bounds ───────────────────────────────────────────────────────────────────
def bounds(rings):
    """(min_lat, min_lon, max_lat, max_lon) over already-unwrapped rings.

    The longitudes may run past ±180; that is the point. Storing the true
    window is what lets `align_lon` recognise a crossing region later.
    """
    lats = [p[1] for ring in rings for p in ring]
    lons = [p[0] for ring in rings for p in ring]
    if not lats:
        raise ValueError('No points to bound.')
    return min(lats), min(lons), max(lats), max(lons)


def centroid(min_lat, min_lon, max_lat, max_lon):
    """Box centre, with the longitude wrapped back for display.

    The centroid is only ever used to frame a map, and a map wants ±180. The
    bounds it came from stay unwrapped.
    """
    return (min_lat + max_lat) / 2, wrap_lon((min_lon + max_lon) / 2)


# ─── Containment ──────────────────────────────────────────────────────────────
def ring_contains(ring, lat, lon):
    """Even-odd ray cast against one ring of [lon, lat] pairs.

    Counts crossings of a ray running east from the point. The
    `(y1 > lat) != (y2 > lat)` test picks exactly one of the two edges meeting
    at a vertex, so a ray passing precisely through a vertex is not counted
    twice.

    `lon` must already be aligned to the ring's window — see `align_lon`.
    """
    count = len(ring)
    if count < 3:
        return False

    inside = False
    j = count - 1
    for i in range(count):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[j][0], ring[j][1]
        if (y1 > lat) != (y2 > lat):
            # x of the edge where it crosses this latitude
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
        j = i
    return inside


def rings_contain(rings, lat, lon):
    """Whether a point falls inside a ring set.

    A point inside an odd number of rings is inside the region, which makes
    holes fall out for free rather than needing a second pass.
    """
    inside = False
    for ring in rings:
        if ring_contains(ring, lat, lon):
            inside = not inside
    return inside


def contains_within(rings, min_lat, min_lon, max_lat, max_lon, lat, lon, tolerance_m):
    """Whether a point could be inside, allowing for a known position error.

    Exists because the position the server holds for somebody is not always the
    position they are standing in. A user on Approximate sharing has their
    coordinates snapped to a ~500m grid before storage, deliberately and
    irreversibly — so the stored point can sit a few hundred metres from the
    truth, and district borders in a city run down streets. Testing that
    fuzzed point strictly told people standing in Бостандық that they were not
    in Бостандық.

    So the test becomes "could they be inside", probed at the centre and eight
    compass points at `tolerance_m`. That is coarse on purpose: it is not
    measuring a distance to the boundary, it is asking whether the uncertainty
    the user's own privacy setting introduced could account for the miss.

    With `tolerance_m` of zero this is exactly `contains`, so nothing changes
    for anyone sharing a precise location.
    """
    if contains(rings, min_lat, min_lon, max_lat, max_lon, lat, lon):
        return True
    if tolerance_m <= 0:
        return False

    lat_delta = tolerance_m / 111_320.0
    lon_delta = tolerance_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    for dy, dx in (
        (1, 0), (-1, 0), (0, 1), (0, -1),
        (0.7071, 0.7071), (0.7071, -0.7071), (-0.7071, 0.7071), (-0.7071, -0.7071),
    ):
        if contains(
            rings, min_lat, min_lon, max_lat, max_lon,
            lat + dy * lat_delta, lon + dx * lon_delta,
        ):
            return True
    return False


def contains(rings, min_lat, min_lon, max_lat, max_lon, lat, lon):
    """Full containment test: box first, then the rings.

    The box rejects almost every candidate with four comparisons, so the ring
    walk only runs for points that are plausibly inside. `lon` arrives in the
    ordinary ±180 range and is aligned to the region's window first — without
    that step, a point at -179.9 is nowhere near a Fiji box of [178.2, 180.4]
    despite standing in the middle of it.
    """
    if not min_lat <= lat <= max_lat:
        return False
    lon = align_lon(lon, min_lon, max_lon)
    if not min_lon <= lon <= max_lon:
        return False
    return rings_contain(rings, lat, lon)
