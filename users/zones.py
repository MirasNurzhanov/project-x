"""The one place that answers "is this location inside this user's zone?".

Every feature that ever needs to know — discovery, feeds, notifications, group
visibility, background sweeps — asks here. The point of a single entry is that
zone semantics live in exactly one function: when a fourth zone type arrives, or
the boundary rules change, nothing else has to be found and edited.

Nothing consumes this yet, and that is deliberate. Before this module the three
zone shapes were stored honestly but never *asked about* — the geometry existed
and no code path called it. Wiring a consumer changes who can see whom, which is
a product decision per feature, so consumers opt in one at a time rather than
this landing as a silent behaviour change.

The type decides the test, and only the type:

    circle   → geographic distance from the user's own position
    district → point-in-polygon against the district boundary
    city     → point-in-polygon against the city boundary

`zone_radius` and `zone_region` both persist across a type change on purpose —
switching to a city and back must not lose the radius you had dialled in — so
containment reads `zone_type` and never infers from which of them is populated.
That is what makes `circle → city` stop using the old radius the instant it is
saved, with no extra clean-up step to forget.

A boundary that cannot be resolved raises rather than degrading to a circle. A
city zone that quietly became a 500m circle because its Region row was evicted
is worse than an error: it silently shrinks someone's zone by three orders of
magnitude and nothing anywhere says so.
"""

from proximity.utils import haversine

#: Machine-readable code for the "we cannot answer this" case, so an API can
#: hand the client something to branch on rather than a sentence to match.
BOUNDARY_NOT_AVAILABLE = 'boundary_not_available'


class ZoneBoundaryUnavailable(Exception):
    """The zone's shape is not knowable, so containment has no answer.

    Raised rather than returning False, because False means "outside" and every
    caller would treat it that way — hiding a broken zone as an empty one.

    Reachable in normal operation: `User.zone_region` is SET_NULL, so evicting
    a cached Region leaves a city zone pointing at nothing. That is a real state
    the app has to handle, not an invariant violation.
    """

    code = BOUNDARY_NOT_AVAILABLE

    def __init__(self, detail='This zone has no boundary available.'):
        self.detail = detail
        super().__init__(detail)


def is_location_inside_zone(user, lat, lon, *, tolerance_m=0.0) -> bool:
    """Whether (`lat`, `lon`) falls inside `user`'s zone.

    `tolerance_m` widens the zone by a known position error. Pass it when the
    coordinates being tested are ones the server *stores* rather than ones
    somebody is standing on — an approximate sharer's position is a ~500m grid
    cell centre, and testing that strictly puts people outside boundaries they
    are demonstrably within. Zero for a precise position, which is the default.

    Raises ZoneBoundaryUnavailable when the zone has no usable shape.
    """
    from .models import User

    zone_type = user.zone_type

    if zone_type == User.ZoneType.CIRCLE:
        # The circle is centred on the user's own position, so a user with no
        # position has no circle — not an empty one.
        if user.latitude is None or user.longitude is None:
            raise ZoneBoundaryUnavailable(
                'This zone follows your position, and Aura does not have one for you yet.'
            )
        metres = haversine(
            float(user.latitude), float(user.longitude), float(lat), float(lon),
        ) * 1000
        return metres <= user.zone_radius + tolerance_m

    if zone_type in (User.ZoneType.DISTRICT, User.ZoneType.CITY):
        region = user.zone_region
        if region is None:
            raise ZoneBoundaryUnavailable(
                f'This zone covers a {zone_type}, and its boundary is no longer available.'
            )
        # Both paths go through regions.geometry, which does the bbox reject
        # first and then even-odd ray casting per ring — holes fall out for
        # free, and a boundary crossing the antimeridian is aligned to the
        # region's own window before anything is compared.
        if tolerance_m:
            return region.could_contain(float(lat), float(lon), tolerance_m)
        return region.contains(float(lat), float(lon))

    # Unreachable through the field's own choices; a guard rather than a
    # silent False, which would read as "outside" for a type we cannot test.
    raise ZoneBoundaryUnavailable(f'Unknown zone type {zone_type!r}.')


def zone_boundary_available(user) -> bool:
    """Whether `is_location_inside_zone` can answer for this user at all.

    For callers that would rather skip a user than handle the exception — a
    sweep over many rows, say. Deliberately not folded into the containment
    call: a caller that has not thought about the broken case should get the
    exception, not a quiet False.
    """
    from .models import User

    if user.zone_type == User.ZoneType.CIRCLE:
        return user.latitude is not None and user.longitude is not None
    return user.zone_region_id is not None
