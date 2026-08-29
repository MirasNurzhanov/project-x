"""Who can see which club, and from where.

One rule, in one place, applied by every endpoint that can surface a club:

    a club is visible to you when you are standing inside the place it was
    scoped to — its district, its part of town, or its city — and never
    outside the city, whatever else is true.

Three things about how that is enforced are worth reading before changing
anything here.

**It is a queryset filter, not a check after the fact.** `visible_to` narrows
the SQL, so a club outside your area is not fetched, not counted, not
serialised, and cannot leak through a field somebody adds later. `can_see` is
the same rule written for one object, and exists for the single-club paths and
for the tests; the two are kept in step by `test_visibility_matches_filter`.

**An unknown location is a "no".** Not knowing where somebody is looks exactly
like being outside every area, and it has to: the alternative is a fallback
that shows everything when the district lookup happens to fail, which is the
one outcome this feature exists to prevent. Callers surface that state to the
user — see `Unresolved` — rather than silently returning an empty list.

**Membership outranks location, and only membership.** Once you are in a club
you keep it wherever you go: its chat, its events, its page, and its place in
your list. That is a deliberate product rule, not an oversight — a club is a
group of people, and a weekend away is not a reason to be thrown out of one.
What membership does *not* do is put an out-of-area club back into discovery:
you will not be shown clubs you could not have joined from here, and a club
you left in a city you have left does not come back. Owners and moderators
are members by construction, so managing a club never depends on standing in
it.
"""
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from regions import places
from users import visibility as user_visibility

from .models import Club, ClubMember

#: How old the position we hold may be before it stops admitting anyone to
#: anywhere.
#:
#: Borrowed from `users.visibility`, not declared. There is one answer in this
#: app to "how old is too old for a position", and clubs holding a second copy
#: of it is how the two drift apart — this used to be six hours while the map
#: ran on fifteen minutes, which meant a person could be too stale to draw on a
#: map and still fresh enough to pick their city.
#:
#: `CLUB_POSITION_MAX_AGE_S` still overrides it, so the two *can* be separated
#: deliberately if club scoping ever wants a longer memory than the map. It
#: takes a setting to do it rather than an unnoticed edit.
DEFAULT_POSITION_MAX_AGE_S = user_visibility.DEFAULT_POSITION_MAX_AGE_S


def _max_age():
    return getattr(
        settings, 'CLUB_POSITION_MAX_AGE_S', user_visibility.position_max_age_s(),
    )


#: A clause that matches no row, spelled as an impossible condition on a column
#: that cannot be null. Every branch below starts here and is widened by the
#: identifiers we actually hold, so a tier we know nothing about contributes
#: nothing — rather than degrading into an `IS NULL` that would match every
#: club that never recorded one.
_NOTHING = Q(pk__isnull=True)


class Unresolved:
    """Why we cannot place a user, in the words the app shows them.

    A distinct type rather than None so an endpoint cannot accidentally treat
    "we do not know where you are" as "you are nowhere and that is fine". Every
    caller either surfaces `.detail` or fails closed; there is no third option.
    """

    #: Machine-readable, so the client can offer the right button.
    NO_LOCATION = 'no_location'
    STALE = 'stale_location'
    INACCURATE = 'inaccurate_location'
    UNMAPPED = 'unmapped_location'

    MESSAGES = {
        NO_LOCATION: (
            'Approx needs your location to show clubs near you. Turn on location '
            'access and open the map for a moment.'
        ),
        STALE: (
            'Approx has not had your location for a while. Open the map for a '
            'moment so it can catch up.'
        ),
        INACCURATE: (
            'Your location is not precise enough to tell which area you are in. '
            'Move somewhere with a clearer view of the sky and try again.'
        ),
        UNMAPPED: (
            'Approx could not work out which city you are in, so it cannot show '
            'clubs scoped to one yet.'
        ),
    }

    def __init__(self, reason, detail=''):
        self.reason = reason
        self.detail = detail or self.MESSAGES.get(reason, self.MESSAGES[self.NO_LOCATION])

    @property
    def resolved(self):
        return False

    def as_payload(self):
        return {'location_status': self.reason, 'detail': self.detail}


def _position_age(user):
    """Seconds since the position we hold was reported, or None if unknowable.

    `location_updated_at` is the honest answer and `updated_at` the fallback
    for rows written before that column existed — an upper bound on the age
    rather than the age, which is the right direction to be wrong in: it can
    only make a position look *newer* than it is for users who have not touched
    the app since the migration, and their next location report corrects it.
    """
    stamp = user.location_updated_at or user.updated_at
    if stamp is None:
        return None
    return (timezone.now() - stamp).total_seconds()


def place_for(user, allow_upstream=False, force=False):
    """Where `user` is, or an `Unresolved` saying why we cannot tell.

    The gate every club query goes through. Cache-only by default: this runs on
    list, search and detail requests, and none of those may block on a
    geocoder.
    """
    if user.latitude is None or user.longitude is None:
        return Unresolved(Unresolved.NO_LOCATION)

    age = _position_age(user)
    if age is not None and age > _max_age():
        return Unresolved(Unresolved.STALE)

    if places.position_of(user) is None:
        # A fix too coarse to place anyone. `position_of` is the only judge of
        # that, so the threshold lives in one place.
        return Unresolved(Unresolved.INACCURATE)

    place = places.snapshot_for(user, allow_upstream=allow_upstream, force=force)
    if place is None:
        return Unresolved(Unresolved.NO_LOCATION)
    if not place.resolved:
        return Unresolved(Unresolved.UNMAPPED, place.detail)
    return place


# ─── The rule ─────────────────────────────────────────────────────────────────
# Both implementations below ask the same question of every tier they check:
# *is this user inside the boundary the club named?* — not *is that boundary
# sitting in the same tier slot as it was when the club was made?*
#
# The distinction is the whole robustness of the feature. Tiers are positions
# in a containment chain, so caching one new boundary can move a shape from the
# region slot to the district slot; a club scoped to the old slot would then
# match nobody, silently, and only in the places where the map improved.
# Membership of the chain cannot move like that. Somebody inside Bostandyk
# yesterday is inside it today.


def can_see(club, place):
    """Whether one club is visible from one place.

    The object-level twin of `visible_to`. Reads top to bottom as the rule
    itself: outside the city, no; scoped to the city, yes; otherwise you have
    to be inside the exact area it named, and a club whose area was never
    recorded is inside nobody's chain.
    """
    if club.visibility_level == Club.Visibility.GLOBAL:
        return True

    if place is None or not getattr(place, 'resolved', False):
        return False

    if not places.within(place, places.identity(club, 'city', 'visibility_')):
        return False

    if club.visibility_level == Club.Visibility.CITY:
        return True

    if club.visibility_level in (Club.Visibility.REGION, Club.Visibility.DISTRICT):
        return places.within(
            place, places.identity(club, club.visibility_level, 'visibility_'),
        )

    return False


def _within_clause(tier, place):
    """Clubs whose `tier` names a boundary this place is inside.

    Both identifiers are asked because they fail at different times: the
    primary key dies when the region row is evicted from the shared cache, and
    the OSM reference survives that — the same boundary re-fetched tomorrow
    comes back under a new key carrying the same 'R1234'. Neither is a name;
    see regions.places for why there is no third branch here.
    """
    ids = [i for i in (place.area_ids or []) if i]
    refs = [r for r in (place.area_osm_refs or []) if r]

    match = _NOTHING
    if ids:
        match |= Q(**{f'visibility_{tier}_id__in': ids})
    if refs:
        match |= Q(**{f'visibility_{tier}_osm__in': refs})
    return match


def visible_to(queryset, place):
    """Narrow a club queryset to what this place may see.

    Fails closed twice over: an unresolved place leaves only the legacy clubs,
    and a club scoped to a boundary nobody is inside matches nothing.
    """
    legacy = Q(visibility_level=Club.Visibility.GLOBAL)

    if place is None or not getattr(place, 'resolved', False):
        return queryset.filter(legacy)

    in_city = _within_clause('city', place)
    scoped = in_city & (
        Q(visibility_level=Club.Visibility.CITY)
        | (Q(visibility_level=Club.Visibility.REGION) & _within_clause('region', place))
        | (Q(visibility_level=Club.Visibility.DISTRICT) & _within_clause('district', place))
    )
    return queryset.filter(legacy | scoped)


# ─── Access ───────────────────────────────────────────────────────────────────
def is_member(club, user):
    return ClubMember.objects.filter(club=club, user=user).exists()


def can_access(club, user, place):
    """Whether `user` may open this club at all.

    Wider than `can_see` by exactly one rule — membership — and that difference
    is the documented product decision: location governs *discovery*, and
    belonging governs *access*. See the module docstring.
    """
    return can_see(club, place) or is_member(club, user)


def accessible_to(queryset, user, place):
    """`visible_to`, plus the clubs this user has already joined."""
    return queryset.filter(
        Q(pk__in=visible_to(queryset, place).values('pk')) | Q(members__user=user)
    ).distinct()


# ─── Writing a club's scope ───────────────────────────────────────────────────
def scope_fields(level, place):
    """The stored form of "this club lives here", from a resolved place.

    Everything narrower than the chosen level is recorded too, and deliberately:
    a city-level club still knows which district it was started in, which is
    what a future "clubs near me" ordering would need and what makes an
    accidental widening visible in the data rather than silent.

    Each tier is stored three times over — key, OSM reference, name — because
    the three answer different questions. The first two identify the boundary
    for as long as either survives; the name is for showing a person where the
    club is and is never matched on.
    """
    fields = {
        'visibility_level': level,
        'visibility_country_code': (place.country_code or '')[:2],
    }
    for tier in places.TIERS:
        fields[f'visibility_{tier}'] = getattr(place, tier)
        fields[f'visibility_{tier}_osm'] = getattr(place, f'{tier}_osm', '') or ''
        fields[f'visibility_{tier}_name'] = (
            getattr(place, f'{tier}_name', '') or ''
        )[:200]
    return fields


def available_levels(place):
    """Which levels a creator standing at `place` may choose, narrowest first.

    Only tiers we actually hold a boundary for: offering "district" where no
    district is mapped would produce a club nobody — including its creator on
    their next visit — could reliably be found inside.
    """
    if place is None or not getattr(place, 'resolved', False):
        return []
    return [
        level for level in Club.SELECTABLE
        if getattr(place, level, None) is not None
    ]
