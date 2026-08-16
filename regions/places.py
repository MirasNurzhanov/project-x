"""Where somebody is, in three nested sizes: city, region, district.

The zone feature only ever needed two sizes and could ask for either one on its
own. Club visibility needs all three *at once*, and needs them to agree between
two different people asked at two different times — the creator who scoped a
club to Orbita, and the stranger standing in Orbita a week later. Two
independent lookups cannot promise that. A containment chain can.

So the tiers are not three separate classifications. They are one chain:

    city                     ← the smallest mapped *city* containing the point
      region                 ← the widest mapped area inside that city
        district             ← the narrowest, when it is not the same shape

Read that as an ordering, not a taxonomy. Whether OSM calls the middle one a
raion, a borough, a ward or a quarter does not matter here and is not asked;
what matters is that everybody standing in the same place gets the same three
rows, because containment is a fact about geometry rather than a judgement
about names.

Consequences worth stating plainly:

  * **Two tiers is a normal answer.** Where only one sub-city area is mapped it
    fills the region tier and there is no district tier. The club-creation
    screen then offers two choices instead of three, which is honest — we
    cannot police a boundary we do not have.
  * **Only verified geometry counts.** `regions.resolver.cached_regions` will
    show somebody their own unverified traced boundary; that is right for a
    zone, which is theirs, and wrong here, where it would put the creator and
    the viewer in different districts. Nothing in this module passes a viewer.
  * **The read path never touches the network.** Working out a viewer's tiers
    happens on every club query, so it runs cache-only. Upstream is reached
    only when somebody explicitly asks — opening the create screen, or tapping
    "update my location" — and whatever that costs is paid once for everyone
    who follows them into that city.
"""
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from . import admin_levels, nominatim, overpass, resolver, upstream
from .models import Region, UserPlace

logger = logging.getLogger(__name__)


def log_point(lat, lon):
    """A coordinate coarse enough to put in a log line.

    Two decimal places is about a kilometre — enough to answer "which city did
    this break in", which is the only thing these warnings are ever read for,
    and not enough to place a person. Application logs outlive the request and
    get shipped somewhere; a user's exact position should not be in them, and
    the private browsing path (proximity.views.NearbyQueryView) makes that
    sharper still because a paused user's coordinate is never stored anywhere
    else at all.
    """
    return f'{lat:.2f},{lon:.2f}'


#: The three tiers, widest first. Anything that walks them — a serializer, a
#: picker, a comparison — walks them in this order.
TIERS = ('city', 'region', 'district')

# ─── Tunables ─────────────────────────────────────────────────────────────────
#: How long a snapshot stands before it is recomputed even for somebody who has
#: not moved. Short, because the recompute is two indexed queries and the thing
#: it catches is a *finer* boundary having been cached since — a district that
#: appeared under your feet when a neighbour created a club there.
DEFAULT_SNAPSHOT_TTL_S = 1800
#: An unresolved snapshot is retried sooner, but not on every request: a user
#: standing somewhere OSM has never heard of would otherwise re-run the whole
#: cache lookup on every club query, forever.
DEFAULT_UNRESOLVED_TTL_S = 300
#: A fix this bad is not a location. Past it the snapshot is refused outright
#: rather than resolved from a point that could be a suburb away.
DEFAULT_MAX_ACCURACY_M = 2000.0
#: How deep the chain sweep looks. 11 is OSM's floor for administrative
#: boundaries; below it are things like building blocks, which nobody calls
#: their district and which are mapped as places rather than boundaries anyway.
DEFAULT_MAX_ADMIN_LEVEL = 11


def _conf(name, default):
    return getattr(settings, f'REGION_{name}', default)


# ─── The answer ───────────────────────────────────────────────────────────────
@dataclass
class Hierarchy:
    """Three nested places, any of which may be missing below the city — plus
    every containing boundary, which is the part visibility actually runs on.

    The tiers answer "where am I", which is a question with three slots because
    that is how many a person can usefully choose between. `areas` answers
    "what am I inside", which has as many answers as the map has shapes there,
    and does not move when a new one is cached.
    """

    city: Region = None
    region: Region = None
    district: Region = None

    city_name: str = ''
    region_name: str = ''
    district_name: str = ''
    country_code: str = ''

    #: Every stored boundary containing the point, tightest first. Always a
    #: superset of the three tiers above.
    areas: list = None

    outcome: str = ''
    detail: str = ''

    def __post_init__(self):
        if self.areas is None:
            self.areas = []

    @property
    def resolved(self) -> bool:
        """Whether this can decide anything.

        The city is the floor. Every location-restricted club names one, so
        without it there is no comparison to make and the honest answer to
        "can this person see that club" is "we do not know" — which callers
        must turn into "no", never into "yes".
        """
        return self.city is not None


_UNRESOLVED_DETAIL = (
    'Aura could not work out which area you are in. Open the map for a moment '
    'so it can catch up, or check that location access is on.'
)


# ─── Building the chain ───────────────────────────────────────────────────────
def _inside(city, candidate):
    """Whether `candidate` is a sub-area of `city` rather than the city itself
    or something containing it.

    Two tests, both cheap. The area comparison rejects the province the city
    sits in; the centroid test rejects a neighbouring area whose bounding box
    merely overlaps. Neither is a name comparison, on purpose — "Almaty" the
    city and "Almaty" the region are different shapes with the same word.
    """
    if candidate.pk == city.pk:
        return False
    if candidate.box_area >= city.box_area:
        return False
    return city.contains(candidate.centroid_lat, candidate.centroid_lon)


def _country_hint(lat, lon, tolerance_m):
    """The country the cache thinks this point is in.

    Only matters on the cache-only path, where nothing has asked upstream what
    country this is — and without a country, `regions.admin_levels` reads
    Almaty's level 4 as a province and vetoes the city. Every stored region
    carries the country it was fetched with, so the answer is already here.
    """
    for kind in (Region.Kind.CITY, Region.Kind.DISTRICT):
        for region in resolver.cached_regions(lat, lon, kind, None, tolerance_m):
            if region.country_code:
                return region.country_code
    return ''


def _tightest_city(lat, lon, tolerance_m):
    """The smallest stored *city* containing the point, or None.

    The fallback for when `resolve` declines to name a city it can see. That
    caution exists to stop a wrong label being written into the cache and
    served forever; nothing is written here, and refusing to place somebody in
    a city whose boundary we are holding would just mean showing them no clubs
    at all.

    Restricted to rows positively classified as a city — an unlabelled `other`
    could be anything, and quietly promoting one to "your city" is the mistake
    this whole area of the codebase is arranged around.
    """
    for region in resolver.cached_regions(lat, lon, Region.Kind.CITY, None, tolerance_m):
        if region.kind == Region.Kind.CITY:
            return region
    return None


def _sub_areas(city, lat, lon, tolerance_m):
    """Every stored area inside `city` containing the point, narrowest first."""
    return [
        region
        for region in resolver.cached_regions(
            lat, lon, Region.Kind.DISTRICT, None, tolerance_m,
        )
        if _inside(city, region)
    ]


def _deepen(lat, lon, country):
    """Fetch every sub-city boundary OSM has here, not just the best one.

    The resolver stops as soon as it is confident, which is right when the
    question is "which district am I in" and wrong when the question is "how
    many nested areas are there". In a city where the raion resolves from the
    address hierarchy at stage 3, the microdistrict inside it is never fetched
    at all — so a creator standing in Orbita would be offered Bostandyk as the
    narrowest option and told no district is mapped, when one is.

    So this asks Overpass the one question it is best at — which administrative
    relations enclose this point — across every level the country uses at or
    below its district levels, and caches the lot. It runs only on the explicit
    paths (the create screen, the refresh button), and its answer is cached
    upstream by coordinate for a week, so a city pays for it roughly once.
    """
    if not overpass.is_enabled():
        return

    district_levels = admin_levels.levels_for(country, Region.Kind.DISTRICT)
    # From the country's coarsest district level down to OSM's floor. A level
    # above that is the city or bigger, which the city tier already covers.
    levels = sorted({
        *district_levels,
        *range(min(district_levels), _conf('MAX_ADMIN_LEVEL', DEFAULT_MAX_ADMIN_LEVEL) + 1),
    })

    try:
        boundaries = overpass.boundaries_at(lat, lon, levels)
    except upstream.UpstreamError as exc:
        # A shallower chain is a narrower menu, not a failed lookup.
        logger.warning('Chain deepening failed at %s: %s', log_point(lat, lon), exc)
        return
    if not boundaries:
        return

    # Only the ones we do not already hold — `nominatim.lookup` is the
    # expensive half and there is no point re-fetching a cached boundary.
    known = set(
        Region.objects.filter(
            osm_id__in=[b.osm_id for b in boundaries if b.osm_id],
        ).values_list('osm_id', flat=True)
    )
    missing = [b.osm_ref for b in boundaries if b.osm_id not in known]
    if not missing:
        return

    try:
        nominatim.lookup(missing)
    except upstream.UpstreamError as exc:
        logger.warning('Could not fetch deeper boundaries at %s: %s', log_point(lat, lon), exc)


def hierarchy_for_point(lat, lon, allow_upstream=True, tolerance_m=0.0):
    """The three tiers containing a point, and everything containing it.

    With `allow_upstream` the lookups run first — not for their answers,
    particularly, but for their side effect: they fetch and store the
    boundaries, after which the chain below can be read straight out of the
    cache. That is why the expensive path is worth paying once per city.
    """
    country = _country_hint(lat, lon, tolerance_m)
    city_result = resolver.resolve(
        lat, lon, Region.Kind.CITY,
        viewer=None, allow_upstream=allow_upstream, country_hint=country,
    )
    country = city_result.country_code or country

    city = city_result.region or _tightest_city(lat, lon, tolerance_m)
    if city is None:
        return Hierarchy(
            city_name=city_result.city_name or '',
            country_code=country,
            outcome=city_result.outcome.value,
            detail=city_result.detail or _UNRESOLVED_DETAIL,
        )

    if allow_upstream:
        # Warm the cache: the sub-city boundaries have to exist as rows before
        # the chain can find them. The resolver's own answer is discarded —
        # which tier a shape belongs to is decided by containment below, not by
        # this lookup's opinion of what a district is.
        try:
            resolver.resolve(
                lat, lon, Region.Kind.DISTRICT,
                viewer=None, allow_upstream=True, country_hint=country,
            )
        except upstream.UpstreamError as exc:
            # A missing district tier is a narrower menu, not a failed lookup.
            logger.warning('District warm-up failed at %s: %s', log_point(lat, lon), exc)
        _deepen(lat, lon, country or city.country_code)

    chain = _sub_areas(city, lat, lon, tolerance_m)
    region = chain[-1] if chain else None
    district = chain[0] if len(chain) >= 2 else None

    return Hierarchy(
        city=city,
        region=region,
        district=district,
        city_name=city.name,
        region_name=region.name if region else '',
        district_name=district.name if district else '',
        country_code=country or city.country_code,
        # Tightest first, and the city last: an outer shape can only ever be
        # the city or something bigger, and nothing bigger is stored as a tier.
        areas=chain + [city],
        outcome=city_result.outcome.value,
        detail='',
    )


# ─── The user's snapshot ──────────────────────────────────────────────────────
def position_of(user):
    """The coordinates to resolve `user` against, with the error we know about.

    Returns `(lat, lon, tolerance_m)`, or None when there is nothing usable.
    The tolerance is the larger of two errors we can name: the fuzz an
    Approximate sharer's own privacy setting introduced, and whatever the phone
    said about its fix. A fix worse than the cap is not a position at all.
    """
    from users import visibility as user_visibility

    if user.latitude is None or user.longitude is None:
        return None

    accuracy = getattr(user, 'location_accuracy_m', None) or 0.0
    if accuracy > _conf('MAX_ACCURACY_M', DEFAULT_MAX_ACCURACY_M):
        return None

    tolerance = max(user_visibility.position_tolerance_m(user), float(accuracy))
    return float(user.latitude), float(user.longitude), tolerance


def _still_current(place, lat, lon, tolerance_m):
    """Whether a stored snapshot still describes where this position is.

    Two ways to stay current, and the second is the one that matters at a
    border. An unchanged position is trivially still in the same place. A
    position that *has* moved is still in the same place as long as it has not
    left the narrowest area we recorded — which is also what stops a user
    walking along a district boundary from flipping tiers on every poll, since
    leaving requires clearing the old shape rather than merely being nearer the
    new one.
    """
    if place.latitude is None or place.longitude is None:
        return False

    age = (timezone.now() - place.resolved_at).total_seconds()
    ttl = _conf(
        'SNAPSHOT_TTL_S' if place.resolved else 'UNRESOLVED_TTL_S',
        DEFAULT_SNAPSHOT_TTL_S if place.resolved else DEFAULT_UNRESOLVED_TTL_S,
    )
    if age > ttl:
        return False

    if abs(place.latitude - lat) < 1e-9 and abs(place.longitude - lon) < 1e-9:
        return True

    narrowest = place.district or place.region or place.city
    if narrowest is None:
        # An unresolved snapshot says nothing about where its point was, so a
        # moved point has to be re-asked.
        return False
    return narrowest.could_contain(lat, lon, tolerance_m)


def snapshot_for(user, allow_upstream=False, force=False):
    """The user's cached hierarchy, recomputed when it no longer fits.

    Returns a `UserPlace` — possibly an unresolved one, which is a real answer
    and is stored as such so the next request does not repeat the work — or
    None when there is no position to resolve at all.
    """
    position = position_of(user)
    place = UserPlace.objects.filter(user=user).select_related(
        'city', 'region', 'district',
    ).first()

    if position is None:
        # Deliberately left alone rather than blanked. The last known place is
        # not being *used* — every caller checks the position first — and
        # keeping it means somebody who pauses sharing for an hour does not pay
        # for a full re-resolve when they turn it back on.
        return None

    lat, lon, tolerance = position
    if place and not force and _still_current(place, lat, lon, tolerance):
        return place

    hierarchy = hierarchy_for_point(lat, lon, allow_upstream=allow_upstream,
                                    tolerance_m=tolerance)
    return save_snapshot(user, hierarchy, lat, lon)


def save_snapshot(user, hierarchy, lat, lon):
    """Persist a computed hierarchy as this user's place."""
    place, _ = UserPlace.objects.update_or_create(
        user=user,
        defaults={
            'city': hierarchy.city,
            'region': hierarchy.region,
            'district': hierarchy.district,
            'city_name': hierarchy.city_name[:200],
            'region_name': hierarchy.region_name[:200],
            'district_name': hierarchy.district_name[:200],
            'city_osm': hierarchy.city.osm_ref if hierarchy.city else '',
            'region_osm': hierarchy.region.osm_ref if hierarchy.region else '',
            'district_osm': hierarchy.district.osm_ref if hierarchy.district else '',
            'country_code': (hierarchy.country_code or '')[:2],
            'area_ids': [area.pk for area in hierarchy.areas],
            'area_osm_refs': [a.osm_ref for a in hierarchy.areas if a.osm_ref],
            'latitude': lat,
            'longitude': lon,
            'outcome': hierarchy.outcome[:20],
            'detail': (hierarchy.detail or '')[:300],
        },
    )
    return place


def refresh_quietly(user):
    """Keep a user's snapshot warm on the back of a location report.

    Best effort in the strictest sense: this rides the endpoint every phone in
    the field hits every thirty seconds, and a place lookup failing there must
    not fail the position write that the map, the zone and the alerts all
    depend on.
    """
    try:
        snapshot_for(user, allow_upstream=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning('Place snapshot failed for user %s: %s', user.pk, exc)


# ─── Matching ─────────────────────────────────────────────────────────────────
# Two identifiers, and deliberately no third.
#
# There was a name comparison here, as the fallback for a region row evicted
# from the shared cache. The OSM reference replaced it and is strictly better,
# but the reason the name path was *removed* rather than demoted is worth
# stating, because it looks like a lost feature and is not one.
#
# A name only ever gets consulted when the boundary itself is gone from the
# database. But if the boundary is gone, nobody's chain contains it — and a
# chain is built by testing points against polygons. So the only thing a name
# match can do at that point is declare somebody inside a shape we no longer
# hold, on the strength of a *different* shape having the same word on it.
# That is not a weaker version of containment; it is a guess dressed as one,
# and it is exactly what "do not compare locations by free-text name" is
# warning about. Where the boundary survives — under a new primary key, after a
# re-fetch — the OSM reference recognises it exactly, which is the case the
# name fallback was really there for.


@dataclass
class PlaceIdentity:
    """One boundary, as the two things that can identify it exactly.

    The primary key is this database's name for the place: exact, and dead the
    moment the row is evicted. The OSM reference is the place's own name for
    itself: equally exact and it survives, because re-fetching the same
    boundary tomorrow produces a new row carrying the same 'R1234'.
    """

    region_id: int = None
    osm_ref: str = ''

    @property
    def known(self) -> bool:
        return bool(self.region_id or self.osm_ref)


def identity(obj, tier, prefix=''):
    """Pull one tier off a snapshot or a club as a `PlaceIdentity`."""
    return PlaceIdentity(
        region_id=getattr(obj, f'{prefix}{tier}_id', None),
        osm_ref=getattr(obj, f'{prefix}{tier}_osm', '') or '',
    )


def within(place, identifier):
    """Whether this place's chain contains the boundary `identifier` names.

    The question visibility actually asks, and the reason it is asked of the
    whole chain rather than of a tier: "am I inside Bostandyk" has a stable
    answer, where "is Bostandyk my region tier" depends on what else has been
    mapped nearby since.
    """
    if not identifier.known:
        return False
    if identifier.region_id and identifier.region_id in (place.area_ids or []):
        return True
    return bool(
        identifier.osm_ref and identifier.osm_ref in (place.area_osm_refs or [])
    )
