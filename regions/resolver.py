"""Working out which district — or city — somebody is standing in.

The naive version of this is one reverse-geocode, and it fails across most of
the world at sub-city level. Not because the boundary is missing, usually, but
because the *name* is: Nominatim's address hierarchy has to place a name at the
level being asked about before there is anything to search for, and how densely
that hierarchy is populated varies enormously by country. A ward in Dar es
Salaam, a phường in Hanoi and a 区 in Osaka are all mapped, and all three used
to come back as "nothing found".

So this is a pipeline, and every stage is a different way of asking:

  1. **Stored polygons.** Free, instant, and the answer for anywhere another
     user has already been.
  2. **Reverse geocode.** Learn what the place is called at every level, not
     just one — `SUBDIVISION_FIELDS` collects all of them.
  3. **Name search.** For each name found, look for a boundary of that name
     near that point.
  4. **Boundary relations, direct.** Ask OSM which administrative relations
     enclose the coordinate, filtered to the levels *this country* uses. This
     is the stage that does not need a name at all, which is why it reaches
     places the first three cannot.

Candidates accumulate across stages and are ranked together. The pipeline stops
as soon as one is a confident winner, so the expensive stages usually never run.

Two rules govern what comes out:

  * **Containment is mandatory, everything else is evidence.** A polygon that
    does not contain the point is not a candidate at any score.
  * **A low-confidence answer is never returned as though it were an answer.**
    Ambiguity is a result, not something to break with a tiebreak nobody can
    see. The caller gets ranked choices instead.

That second rule is the lesson from the admin_level bug written into the
control flow: a wrong region here is not a wrong answer once, it is a wrong
answer forever, because it gets stored and every later lookup is served from
the store.
"""
import logging
import math
from dataclasses import dataclass, field
from enum import Enum

from django.conf import settings
from django.db.models import Q

from . import admin_levels, nominatim, overpass, upstream
from .models import Region

logger = logging.getLogger(__name__)

# ─── Tunables ─────────────────────────────────────────────────────────────────
# The score a candidate must reach before it can be returned without asking.
# Roughly: the right admin level for the country, plus either a name match or a
# parent-city match, plus being the tightest shape available.
DEFAULT_MIN_CONFIDENCE = 45
# How far clear of the runner-up the winner has to be. Below this the two are
# not meaningfully distinguishable and the user is asked.
DEFAULT_AMBIGUITY_MARGIN = 15
# How many of the names found in the address hierarchy get their own search.
# Each is an upstream call, and past the first two or three they are names of
# things far larger than the level being asked about.
DEFAULT_MAX_NAME_SEARCHES = 3
# Ranked choices offered when confidence is insufficient.
DEFAULT_MAX_CANDIDATES = 5


def _conf(name, default):
    return getattr(settings, f'REGION_{name}', default)


# Every address key that could plausibly name a subdivision, most specific
# first. Deliberately longer than the list `nominatim.reverse` uses for its
# single-name path: this is a net, and a name that turns out to be too big is
# cheap — it simply scores badly and loses. A name never collected is a
# district that can never be found.
#
# `municipality` and `county` are near the end because in most countries they
# are a city or larger; they earn their place because in some — parts of the
# UAE, some US metros — the thing somebody calls their district is administered
# as one. `administrative` is not an address key at all and is listed for
# completeness of the documented contract; it reaches classification as an
# addresstype, through `nominatim._hint_applies`.
SUBDIVISION_FIELDS = (
    'city_district', 'district', 'borough', 'ward', 'suburb', 'quarter',
    'neighbourhood', 'municipality', 'county', 'administrative',
)

CITY_FIELDS = ('city', 'town', 'village', 'municipality')

# addresstypes that cannot be either size on offer, whatever else agrees.
NEVER_TYPES = {
    'country', 'continent', 'sea', 'ocean', 'political_union', 'archipelago',
}


class Outcome(str, Enum):
    """How a lookup ended. These are the coverage categories."""

    #: A stored polygon or the reverse geocode answered it outright.
    RESOLVED = 'resolved'
    #: A name search or a direct boundary-relation query answered it.
    RESOLVED_FALLBACK = 'resolved_fallback'
    #: Several plausible answers, none clearly best. The user picks.
    AMBIGUOUS = 'ambiguous'
    #: OSM has no boundary here at this size. Nothing to pick from.
    UNMAPPED = 'unmapped'
    #: Polygons were found and none was trustworthy enough to return.
    REJECTED = 'rejected'


RESOLVED_OUTCOMES = (Outcome.RESOLVED, Outcome.RESOLVED_FALLBACK)


@dataclass
class Candidate:
    """One containing polygon, with why it scored what it scored."""

    region: Region
    score: int = 0
    reasons: list = field(default_factory=list)
    stage: str = ''

    @property
    def area(self):
        return max(
            (self.region.max_lat - self.region.min_lat)
            * (self.region.max_lon - self.region.min_lon),
            1e-9,
        )


@dataclass
class Resolution:
    """What the pipeline concluded, and enough to explain it."""

    outcome: Outcome
    region: Region = None
    candidates: list = field(default_factory=list)
    stage: str = ''
    city_name: str = None
    subdivision_names: list = field(default_factory=list)
    country_code: str = ''
    detail: str = ''

    @property
    def resolved(self):
        return self.outcome in RESOLVED_OUTCOMES


# ─── Context ──────────────────────────────────────────────────────────────────
@dataclass
class _Context:
    lat: float
    lon: float
    kind: str
    country_code: str = ''
    city_name: str = None
    state_name: str = None
    subdivision_names: list = field(default_factory=list)

    @property
    def names(self):
        """Every name worth matching a candidate against."""
        return [n for n in self.subdivision_names if n]


def _same(left, right):
    return bool(left) and bool(right) and left.strip().casefold() == right.strip().casefold()


# ─── Scoring ──────────────────────────────────────────────────────────────────
def _vetoed(region, context):
    """Reasons a polygon cannot be the answer, whatever else it has going for it.

    Vetoes rather than penalties on purpose. A state is not a district that
    happens to score badly — it is not a district — and leaving that to score
    arithmetic is how a confident wrong answer eventually gets produced.
    """
    if admin_levels.is_too_coarse(context.country_code, context.kind, region.admin_level):
        return 'admin level is coarser than this country uses for a %s' % context.kind

    if context.kind == Region.Kind.DISTRICT and _same(region.name, context.city_name):
        # The city itself, surfacing in a district lookup because its name sits
        # in the hierarchy too. Returning it would silently widen the zone by
        # orders of magnitude.
        return 'this is the city, not a district within it'

    return None


def _score(region, context, stage, largest_area):
    """Rank one containing polygon. Higher is better; nothing here is a veto."""
    candidate = Candidate(region=region, stage=stage)
    # Containing the point is mandatory, so it is a floor rather than a
    # discriminator — but it is still evidence, and without it a lone candidate
    # with thin metadata sits at zero and can never clear the bar.
    score = 5

    # Does this country use this admin level for this size?
    rank = admin_levels.level_rank(context.country_code, context.kind, region.admin_level)
    if rank == 0:
        score += 30
        candidate.reasons.append(f'admin level {region.admin_level} is primary for {context.country_code or "??"}')
    elif rank == 1:
        score += 20
        candidate.reasons.append(f'admin level {region.admin_level} is secondary for {context.country_code or "??"}')
    elif rank is not None:
        score += 12
        candidate.reasons.append(f'admin level {region.admin_level} is used for this size')

    # Did the address hierarchy name this exact place at this level? The
    # strongest signal after containment: it means OSM itself, asked about this
    # coordinate, produced this name at this level.
    if any(_same(region.name, name) for name in context.names):
        score += 30
        candidate.reasons.append('name matches the address hierarchy')
    elif any(
        name and region.name and name.casefold() in region.name.casefold()
        for name in context.names
    ):
        score += 10
        candidate.reasons.append('name partially matches the address hierarchy')

    # Does it belong to the city we think we are in?
    if context.city_name and context.city_name.casefold() in (region.display_name or '').casefold():
        score += 12
        candidate.reasons.append(f'sits inside {context.city_name}')

    # Metadata quality: a relation is a real boundary where a way is often a
    # stray closed line, and a stored `kind` is not a guess — it was read off an
    # actual `addresstype` when the region was fetched, which is the same
    # evidence the rest of this module works so hard to corroborate.
    if region.osm_type == Region.OSMType.RELATION:
        score += 10
        candidate.reasons.append('mapped as a boundary relation')
    if region.kind == context.kind:
        score += 25
        candidate.reasons.append(f'classified as a {context.kind}')
    elif region.kind == Region.Kind.OTHER:
        candidate.reasons.append('no usable size label')
    if region.source == Region.Source.OSM:
        score += 10
        candidate.reasons.append('from OpenStreetMap')
    else:
        # Worth more than the OSM bonus, which reads oddly until you follow
        # where the trust actually comes from. A traced shape is only ever a
        # candidate for its own author, or after somebody verified it — the
        # visibility rule in `cached_regions` is the check. By the time one is
        # being scored at all, a human has deliberately drawn it for this exact
        # purpose and it contains the point. What it lacks is the metadata
        # every other signal reads, so without this it could never clear the
        # bar and the whole tracing path would be write-only.
        score += 20
        candidate.reasons.append('traced by hand for this area')

    # Tightness, measured against the largest candidate rather than by rank, so
    # two similarly-sized shapes stay close together and are recognised as the
    # ambiguity they are — while a district inside a province is separated
    # decisively.
    ratio = largest_area / candidate.area
    if ratio > 1:
        tightness = round(20 * min(1.0, math.log10(ratio)))
        if tightness:
            score += tightness
            candidate.reasons.append('tightest shape containing the point')

    candidate.score = score
    return candidate


def _rank(regions, context, stage, existing):
    """Score a batch of regions alongside whatever is already in the pool."""
    pool = {c.region.pk: c for c in existing}
    fresh = [r for r in regions if r and r.pk not in pool]

    keep = []
    for region in fresh:
        if not region.contains(context.lat, context.lon):
            continue
        if (region.kind == Region.Kind.OTHER
                and (region.name or '').strip() == ''):
            continue
        veto = _vetoed(region, context)
        if veto:
            upstream.event(
                'regions', 'candidate_vetoed', osm_id=region.osm_id,
                name=region.name, reason=veto, wanted=context.kind,
            )
            continue
        keep.append(region)

    if not keep and not pool:
        return []

    all_regions = [c.region for c in pool.values()] + keep
    largest = max(
        ((r.max_lat - r.min_lat) * (r.max_lon - r.min_lon)) or 1e-9
        for r in all_regions
    )

    # Everything is rescored: tightness is relative to the pool, so a new
    # arrival changes what the existing candidates are worth.
    rescored = [
        _score(c.region, context, c.stage, largest) for c in pool.values()
    ] + [
        _score(region, context, stage, largest) for region in keep
    ]
    rescored.sort(key=lambda c: (-c.score, c.area))
    return rescored


# ─── Decision ─────────────────────────────────────────────────────────────────
def _decide(candidates, context, stage):
    """Turn a ranked pool into an outcome.

    Never breaks a near-tie. Two candidates within the margin are two answers,
    and picking one silently is precisely the failure this whole module is
    arranged to avoid.
    """
    if not candidates:
        return None

    best = candidates[0]
    minimum = _conf('MIN_CONFIDENCE', DEFAULT_MIN_CONFIDENCE)
    margin = _conf('AMBIGUITY_MARGIN', DEFAULT_AMBIGUITY_MARGIN)
    limit = _conf('MAX_CANDIDATES', DEFAULT_MAX_CANDIDATES)

    if best.score < minimum:
        return Resolution(
            outcome=Outcome.REJECTED,
            candidates=candidates[:limit],
            stage=stage,
            detail=(
                'Nothing here looks reliably like a %s. The closest matches are '
                'offered rather than assumed.' % context.kind
            ),
        )

    runner_up = candidates[1] if len(candidates) > 1 else None
    if runner_up and best.score - runner_up.score < margin:
        return Resolution(
            outcome=Outcome.AMBIGUOUS,
            candidates=candidates[:limit],
            stage=stage,
            detail='More than one place fits. Pick the one you mean.',
        )

    return Resolution(
        outcome=(
            Outcome.RESOLVED if best.stage in ('cache', 'reverse')
            else Outcome.RESOLVED_FALLBACK
        ),
        region=best.region,
        candidates=candidates[:limit],
        stage=best.stage,
    )


# ─── Stages ───────────────────────────────────────────────────────────────────
def cached_regions(lat, lon, kind, viewer=None, tolerance_m=0.0):
    """Stored polygons of this size containing the point, tightest first.

    Bounding box in SQL, exact containment in Python: the box query is indexed
    and throws away all but a handful of rows, and only those get the ring walk.

    A region crossing the antimeridian is stored in a window that runs past
    ±180 (see regions.geometry), so a query at -179.9 has to be offered as
    180.1 as well. The shifted clauses can only match rows whose window leaves
    the ordinary range, which is exactly the crossing ones.

    `tolerance_m` widens both the box and the ring test by a known position
    error — the fuzz an Approximate sharer's own privacy setting introduced, or
    a poor GPS fix — so somebody standing in Бостандық is not told they are
    not. Zero, the default, is exactly the old strict behaviour.
    """
    lat_pad = tolerance_m / 111_320.0 if tolerance_m else 0.0
    lon_pad = (
        tolerance_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
        if tolerance_m else 0.0
    )

    window = Q(min_lon__lte=lon + lon_pad, max_lon__gte=lon - lon_pad)
    for shifted in (lon + 360.0, lon - 360.0):
        window |= Q(min_lon__lte=shifted + lon_pad, max_lon__gte=shifted - lon_pad)

    # A boundary somebody traced by hand is theirs until it is checked. Serving
    # an unverified shape to everyone would let one person redraw a district
    # for the whole city.
    trust = Q(verified=True)
    if viewer is not None and getattr(viewer, 'pk', None):
        trust |= Q(submitted_by=viewer)

    # `other` is included alongside the size asked for, and it matters. A
    # region whose `addresstype` could not be classified is stored as `other`,
    # which describes a great many correctly-mapped districts — and filtering
    # it out here made them permanently invisible however well they fitted.
    # Including them cannot produce a wrong answer on its own: without a size
    # label a candidate does not earn the classification score, so it can only
    # clear the confidence bar with strong corroboration from the level table
    # and the address hierarchy, and otherwise surfaces as a ranked choice.
    sizes = (kind, Region.Kind.OTHER)

    candidates = Region.objects.filter(
        Q(kind__in=sizes, min_lat__lte=lat + lat_pad, max_lat__gte=lat - lat_pad)
        & window & trust
    )
    return sorted(
        (r for r in candidates if r.could_contain(lat, lon, tolerance_m)),
        key=lambda r: (r.max_lat - r.min_lat) * (r.max_lon - r.min_lon),
    )


def _collect_address(context):
    """Stage 2: learn what this place is called at every level we might want.

    The single most valuable thing the pipeline does. One upstream call yields
    the country — which decides what admin levels mean here — the city, and
    every subdivision name the hierarchy carries.
    """
    address = nominatim.address_at(context.lat, context.lon)
    if not address:
        return None

    # A hint only ever fills a blank: upstream's answer wins, but an upstream
    # response that names no country must not erase what the caller knew.
    context.country_code = (address.get('country_code') or '')[:2] or context.country_code
    context.state_name = address.get('state') or address.get('province')
    for key in CITY_FIELDS:
        if address.get(key):
            context.city_name = address[key]
            break

    seen = set()
    for key in SUBDIVISION_FIELDS:
        value = address.get(key)
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            context.subdivision_names.append(value)

    # For a city lookup the city's own name is the thing to match on.
    if context.kind == Region.Kind.CITY and context.city_name:
        context.subdivision_names.insert(0, context.city_name)

    upstream.event(
        'regions', 'address_collected', country=context.country_code or None,
        city=context.city_name, kind=context.kind,
        names='|'.join(context.subdivision_names) or None,
    )
    if not context.subdivision_names:
        # The commonest miss of all, and the one that says nothing about any
        # `addresstype` — because there was no result to have one. Aggregating
        # this by country is how you tell "OSM has no boundary here" apart from
        # "the hierarchy does not name this level here", which are different
        # problems with different fixes.
        upstream.event(
            'regions', 'no_address_name_at_level', kind=context.kind,
            country=context.country_code or None,
            address_keys=','.join(sorted(address)) or None,
        )
    return address


def _search_by_name(context):
    """Stage 3: for each name found, look for a boundary of that name here.

    A generator, so the caller can rank and decide after every single name.
    Each one is an upstream request against a rate-limited service, and the
    first name is the most specific — most of the time nothing after it needs
    to be asked at all.
    """
    budget = _conf('MAX_NAME_SEARCHES', DEFAULT_MAX_NAME_SEARCHES)
    for index, name in enumerate(context.names[:budget]):
        query = name
        if (context.kind == Region.Kind.DISTRICT and context.city_name
                and not _same(name, context.city_name)):
            # "Msasani" alone is ambiguous in a way "Msasani, Dar es Salaam"
            # is not, and it puts the containing candidate first.
            query = f'{name}, {context.city_name}'
        region = nominatim._resolve_boundary(
            query, context.country_code, context.kind,
            context.lat, context.lon, name_hint=name,
        )
        # The first name is the one the reverse geocode pointed straight at, so
        # finding it there is the ordinary path rather than a fallback. Only the
        # names after it are the pipeline casting about, and the coverage
        # reporting draws its line in the same place.
        yield region, ('reverse' if index == 0 else 'name_search')


def _from_boundary_relations(context):
    """Stage 4: ask OSM directly which relations enclose the point.

    The only stage that needs no name, which is exactly why it reaches the
    places the others cannot. Overpass decides *which* relations; the geometry
    still comes from Nominatim, because assembling a relation's border out of
    unordered ways is a second source of bugs for no gain.
    """
    if not overpass.is_enabled():
        return []

    levels = admin_levels.levels_for(context.country_code, context.kind)
    try:
        boundaries = overpass.boundaries_at(context.lat, context.lon, levels)
    except upstream.UpstreamError as exc:
        # A fallback that fails is not a failed lookup. Whatever the earlier
        # stages found still stands.
        logger.warning('region_boundary_fallback_failed', extra={'error_type': type(exc).__name__})
        return []
    if not boundaries:
        return []

    try:
        return nominatim.lookup([b.osm_ref for b in boundaries])
    except nominatim.NominatimError as exc:
        logger.warning('region_boundary_geometry_failed', extra={'error_type': type(exc).__name__})
        return []


# ─── Pipeline ─────────────────────────────────────────────────────────────────
def resolve(lat, lon, kind=Region.Kind.CITY, viewer=None, allow_upstream=True,
            country_hint=''):
    """The place of a given size containing a point, or an honest reason why not.

    Stops at the first stage that produces a confident winner, so the common
    case is one indexed query and the expensive stages stay rare.

    `allow_upstream=False` stops after stage 1 — stored polygons only, no
    network, no rate limiter. That is the mode anything on a read path uses:
    working out which district a viewer is standing in happens on every club
    query, and a lookup that could block on Nominatim has no business there.
    A cache miss then reports UNMAPPED, which callers must read as "not known
    yet" rather than "nowhere", and resolve properly on an explicit request.

    `country_hint` supplies the country the address hierarchy would otherwise
    have told us, which matters only on the cache-only path: without it Almaty
    is *vetoed* as a city, because level 4 is a province everywhere the table
    does not say otherwise. Stage 2 overwrites it with the real thing the
    moment it runs, so it can never override upstream.
    """
    context = _Context(lat=lat, lon=lon, kind=kind, country_code=(country_hint or '')[:2])
    pool = []

    # Stage 1 — what we already hold.
    pool = _rank(cached_regions(lat, lon, kind, viewer), context, 'cache', pool)
    decision = _decide(pool, context, 'cache')
    if decision and decision.resolved:
        return _finish(decision, context)

    if not allow_upstream:
        return _finish(
            decision or Resolution(
                outcome=Outcome.UNMAPPED, stage='cache',
                detail='No stored area covers this location yet.',
            ),
            context,
        )

    # Stage 2 — what this place is called, at every level.
    address = _collect_address(context)
    if address is None and not pool:
        return _finish(
            Resolution(
                outcome=Outcome.UNMAPPED, stage='reverse',
                detail='No mapped area covers this location.',
            ),
            context,
        )

    # Re-rank stage 1 now that the country and the names are known — the same
    # cached rows are worth more once there is something to match them against.
    if pool:
        pool = _rank([c.region for c in pool], context, 'cache', [])
        decision = _decide(pool, context, 'cache')
        if decision and decision.resolved:
            return _finish(decision, context)

    # Stage 3 — search for each name we found, deciding after every one so a
    # confident answer costs a single request rather than the whole budget.
    for region, stage in _search_by_name(context):
        if not region:
            continue
        pool = _rank([region], context, stage, pool)
        decision = _decide(pool, context, stage)
        if decision and decision.resolved:
            return _finish(decision, context)

    # Stage 4 — ask OSM for the relations themselves.
    pool = _rank(_from_boundary_relations(context), context, 'boundary_relations', pool)
    decision = _decide(pool, context, 'boundary_relations')
    if decision:
        return _finish(decision, context)

    return _finish(
        Resolution(
            outcome=Outcome.UNMAPPED,
            stage='boundary_relations',
            detail=(
                f'No mapped {kind} inside {context.city_name}.'
                if kind == Region.Kind.DISTRICT and context.city_name
                else 'No mapped area covers this location.'
            ),
        ),
        context,
    )


def _finish(resolution, context):
    """Attach the context and record the coverage outcome."""
    resolution.city_name = context.city_name
    resolution.subdivision_names = list(context.subdivision_names)
    resolution.country_code = context.country_code
    if not resolution.detail and resolution.outcome is Outcome.UNMAPPED:
        resolution.detail = 'No mapped area covers this location.'

    upstream.event(
        'regions', 'resolved',
        outcome=resolution.outcome.value,
        stage=resolution.stage or None,
        kind=context.kind,
        country=context.country_code or None,
        city=context.city_name,
        candidates=len(resolution.candidates),
        score=resolution.candidates[0].score if resolution.candidates else None,
        chosen=resolution.region.osm_id if resolution.region else None,
    )
    return resolution
