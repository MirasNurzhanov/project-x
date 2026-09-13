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


#: How close to an edge counts as *on* it, in degrees. ~1.1cm of latitude — far
#: below any accuracy a phone can offer, so it only ever catches points that are
#: genuinely on the line, and absorbs the float error in the cross product below
#: rather than widening the boundary by anything that matters.
EDGE_EPSILON_DEG = 1e-7


def point_on_ring(ring, lat, lon, epsilon=EDGE_EPSILON_DEG):
    """Whether a point lies on one of this ring's edges.

    Point-to-segment distance: a dot product locates the foot of the
    perpendicular and rejects points past either end, a cross product gives the
    offset. Degrees are treated as a flat plane, which over one simplified
    boundary segment is far more precision than the epsilon asks for.
    """
    count = len(ring)
    # The same floor `ring_contains` uses, and for the same reason: fewer than
    # three points is not a polygon ring. A two-point ring is a line segment,
    # which encloses nothing and therefore has no boundary to be *on* — without
    # this, a point lying on that line would be reported inside a region of zero
    # area.
    if count < 3:
        return False

    j = count - 1
    for i in range(count):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[j][0], ring[j][1]
        j = i

        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            # Degenerate segment — a repeated vertex. Compare against the point.
            if abs(lon - x1) <= epsilon and abs(lat - y1) <= epsilon:
                return True
            continue

        # Where the perpendicular meets the segment, as a fraction of its
        # length. Outside [0, 1] the nearest point is an endpoint, which the
        # adjoining segment tests anyway.
        t = ((lon - x1) * dx + (lat - y1) * dy) / seg_len_sq
        if t < 0 or t > 1:
            continue
        if abs(dx * (lat - y1) - dy * (lon - x1)) / (seg_len_sq ** 0.5) <= epsilon:
            return True
    return False


def rings_contain(rings, lat, lon):
    """Whether a point falls inside a ring set.

    Boundary-inclusive, matching PostGIS `ST_Covers` rather than `ST_Contains`.
    Without the edge test the answer for a point exactly on the border is
    whatever the ray cast happens to do at that vertex — a convention rather
    than a decision, and one that lets the two sides of a shared border disagree
    about who is standing on it. Somebody on the line is in the region being
    asked about.

    Holes are part of a polygon's boundary, so a point on a hole's edge is
    covered too. That falls out of testing every ring alike, which is why this
    runs as its own pass instead of being folded into the parity count below.

    Otherwise: a point inside an odd number of rings is inside the region, which
    makes holes fall out for free rather than needing a second pass.

    `rings` may also be a `PreparedRings`, which gives the same answer faster.
    """
    if isinstance(rings, PreparedRings):
        return rings.contain(lat, lon)

    for ring in rings:
        if point_on_ring(ring, lat, lon):
            return True

    inside = False
    for ring in rings:
        if ring_contains(ring, lat, lon):
            inside = not inside
    return inside


# ─── Prepared rings ───────────────────────────────────────────────────────────
#: Slack on every band and box cull, far above float error: the index may only over-select
#: edges, and each edge it keeps is decided by exactly the arithmetic of the functions above.
_INDEX_MARGIN_DEG = EDGE_EPSILON_DEG * 4
#: Average edges per latitude band.
_EDGES_PER_BAND = 8
#: Most band entries the index holds per edge. Keeps it linear in the ring's size when edges each
#: span much of the ring's height, where more bands would not shorten any band, only copy edges.
_MAX_BAND_ENTRIES_PER_EDGE = 4


class PreparedRing:
    """One ring indexed by latitude band; answers exactly what `ring_contains` and `point_on_ring` do."""

    __slots__ = ('min_lat', 'max_lat', 'min_lon', 'max_lon', '_base', '_height', '_bands')

    def __init__(self, ring):
        self._bands = None
        count = len(ring)
        if count < 3:
            return

        edges = []
        j = count - 1
        for i in range(count):
            # The same pairing as the ring walks above: (x1, y1) is vertex i, (x2, y2) the one before it.
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[j][0], ring[j][1]
            j = i
            edges.append((x1, y1, x2, y2, min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))

        self.min_lat = min(edge[6] for edge in edges)
        self.max_lat = max(edge[7] for edge in edges)
        self.min_lon = min(edge[4] for edge in edges)
        self.max_lon = max(edge[5] for edge in edges)

        self._base = self.min_lat - _INDEX_MARGIN_DEG
        span = self.max_lat + _INDEX_MARGIN_DEG - self._base
        # An edge lands in at most (its padded height / band height) + 1 bands, so this many bands
        # keeps the total within _MAX_BAND_ENTRIES_PER_EDGE per edge. Any band count answers exactly.
        padded_heights = sum(edge[7] - edge[6] + 2 * _INDEX_MARGIN_DEG for edge in edges)
        band_count = max(1, min(
            count // _EDGES_PER_BAND,
            int((_MAX_BAND_ENTRIES_PER_EDGE - 1) * count * span / padded_heights),
        ))
        self._height = span / band_count
        self._bands = [[] for _ in range(band_count)]
        for edge in edges:
            first = self._band(edge[6] - _INDEX_MARGIN_DEG)
            last = self._band(edge[7] + _INDEX_MARGIN_DEG)
            for band in range(first, last + 1):
                self._bands[band].append(edge)

    def _band(self, lat):
        # Monotonic in `lat`, so an edge spanning a latitude is always filed under that latitude's band.
        return min(max(int((lat - self._base) // self._height), 0), len(self._bands) - 1)

    def on_boundary(self, lat, lon):
        """`point_on_ring`, over only the edges that could lie within an epsilon of the point."""
        if (self._bands is None
                or not self.min_lat - _INDEX_MARGIN_DEG <= lat <= self.max_lat + _INDEX_MARGIN_DEG
                or not self.min_lon - _INDEX_MARGIN_DEG <= lon <= self.max_lon + _INDEX_MARGIN_DEG):
            return False
        epsilon = EDGE_EPSILON_DEG
        for x1, y1, x2, y2, lo_x, hi_x, lo_y, hi_y in self._bands[self._band(lat)]:
            if (lon < lo_x - _INDEX_MARGIN_DEG or lon > hi_x + _INDEX_MARGIN_DEG
                    or lat < lo_y - _INDEX_MARGIN_DEG or lat > hi_y + _INDEX_MARGIN_DEG):
                continue
            dx, dy = x2 - x1, y2 - y1
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq == 0:
                if abs(lon - x1) <= epsilon and abs(lat - y1) <= epsilon:
                    return True
                continue
            t = ((lon - x1) * dx + (lat - y1) * dy) / seg_len_sq
            if t < 0 or t > 1:
                continue
            if abs(dx * (lat - y1) - dy * (lon - x1)) / (seg_len_sq ** 0.5) <= epsilon:
                return True
        return False

    def crossings_odd(self, lat, lon):
        """`ring_contains`, over only the edges of the point's latitude band.

        Outside the ring's latitude span no edge crosses; far east of it none is east of the point;
        far west of it every crossing counts and a closed ring always has an even number of them.
        """
        if (self._bands is None or lat < self.min_lat or lat > self.max_lat
                or lon > self.max_lon + _INDEX_MARGIN_DEG or lon < self.min_lon - _INDEX_MARGIN_DEG):
            return False
        inside = False
        for x1, y1, x2, y2, _lo_x, _hi_x, _lo_y, _hi_y in self._bands[self._band(lat)]:
            if (y1 > lat) != (y2 > lat):
                x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < x_at:
                    inside = not inside
        return inside


class PreparedRings:
    """A ring set indexed once for many containment tests. Same answers as `rings_contain` on the raw rings."""

    __slots__ = ('rings',)

    def __init__(self, rings):
        self.rings = [PreparedRing(ring) for ring in rings]

    def contain(self, lat, lon):
        for ring in self.rings:
            if ring.on_boundary(lat, lon):
                return True
        inside = False
        for ring in self.rings:
            if ring.crossings_odd(lat, lon):
                inside = not inside
        return inside


def prepare(rings):
    """Index `rings` for repeated containment tests; pass the result anywhere `rings` is accepted."""
    return PreparedRings(rings)


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
