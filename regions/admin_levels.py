"""What `admin_level` means, country by country.

OSM's `admin_level` is a depth, not a meaning. The wiki gives a nominal scheme —
2 country, 4 state, 6 county, 8 municipality — and almost every country departs
from it somewhere. Almaty *city* is level 4 and its districts are level 6, which
is the exact collision that once made this feature return a district when asked
for a city.

So nothing here is a lookup from a number to a size in the abstract. It is a
lookup from (country, size) to *the levels that country uses for it*, and it is
only ever consulted once a candidate has already been proved to contain the
point. Read the ordering as: containment decides what is possible, the address
hierarchy decides what is likely, and this decides how to rank what is left.

Two consequences worth stating plainly:

  * A country absent from this table is not broken. It falls back to `DEFAULT`,
    which is the nominal scheme, and everything else in the pipeline — address
    hierarchy, name matching, containment, tightness — still runs. The table
    improves ranking; it is not a prerequisite for an answer.
  * Levels are tuples, most-likely first, never single numbers. Several
    countries genuinely use two or three levels for the same everyday word,
    and picking one would just be the old bug at a finer grain.

Override or extend per deployment with `REGION_ADMIN_LEVELS` in settings; the
value is merged over these, so a single country can be corrected without
restating the rest.
"""
from django.conf import settings

# ─── Per-country levels ───────────────────────────────────────────────────────
# `city` — what somebody means by "my whole city".
# `district` — what somebody means by "my part of it".
#
# Sources are the OSM wiki's per-country admin_level tables. Where a country's
# usage is genuinely contested or thinly mapped, the comment says so rather than
# pretending to a precision the data does not have.

DEFAULT = {
    # The nominal scheme, for countries not listed below.
    'city': (8, 7),
    'district': (9, 10),
}

LEVELS = {
    # England/Wales/Scotland: 6 county or Greater London, 8 district or London
    # borough, 10 civil parish or ward. "My city" is usually the district; in
    # London it is the level above, so both are listed.
    'gb': {'city': (6, 8), 'district': (8, 10)},

    # 6 is the *zila*, which is far larger than a city and is deliberately not
    # offered as one. 8 is the municipal corporation, 10 the ward.
    'in': {'city': (8, 6), 'district': (10, 9)},

    # 4 county (Nairobi is a county), 6 sub-county/constituency, 7 ward.
    'ke': {'city': (4, 6), 'district': (7, 8)},

    # 4 region, 6 district, 7 ward (kata) — the ward is the everyday unit and
    # the one the address hierarchy names.
    'tz': {'city': (6, 4), 'district': (7, 8)},

    # 4 province, 6 district (quận/huyện), 8 ward (phường/xã).
    'vn': {'city': (6, 4), 'district': (8, 9)},

    # 4 prefecture, 7 city/town/village — including Tokyo's special wards,
    # which are cities in their own right — and 8 the ward (区) of a designated
    # city such as Osaka or Yokohama.
    'jp': {'city': (7,), 'district': (8, 9)},

    # 6 county, 8 incorporated place. US neighbourhoods are overwhelmingly
    # mapped as nodes rather than boundaries, so 10 is listed and rarely hits —
    # this is a coverage gap in OSM, not a gap in the mapping.
    'us': {'city': (8,), 'district': (10, 9)},

    # The collision this table exists for: Almaty and Astana are level 4, the
    # same as an oblast, and their districts are 6.
    'kz': {'city': (4, 6), 'district': (6, 7)},

    # 4 emirate, 5/6 municipal region, 7+ sector and community. Community-level
    # boundaries are unevenly mapped and the levels vary between emirates, so
    # this is the least certain row in the table and leans on the other signals.
    'ae': {'city': (4, 6), 'district': (7, 8)},

    # Antimeridian countries, included because their zones exercise the geometry
    # rather than because their levels are unusual.
    'fj': {'city': (8,), 'district': (9, 10)},
    'ki': {'city': (8,), 'district': (9, 10)},
    'nz': {'city': (8,), 'district': (9, 10)},
    'ru': {'city': (6, 4), 'district': (8, 9)},
}


def _table():
    """The levels table with any per-deployment overrides merged in."""
    overrides = getattr(settings, 'REGION_ADMIN_LEVELS', None)
    if not overrides:
        return LEVELS
    merged = {country: dict(rows) for country, rows in LEVELS.items()}
    for country, rows in overrides.items():
        merged.setdefault(country.lower(), {}).update(rows)
    return merged


def levels_for(country_code, kind):
    """The admin levels `country_code` uses for `kind`, most likely first.

    An unknown country gets the nominal scheme rather than nothing, so a
    candidate from an unlisted country is still ranked on the same axis as
    everywhere else — just less sharply.
    """
    country = (country_code or '').lower()
    row = _table().get(country) or DEFAULT
    return tuple(row.get(kind) or DEFAULT[kind])


def level_rank(country_code, kind, admin_level):
    """How well an admin level fits `kind` in this country.

    0 is the country's primary level for that size, 1 the next, and None means
    the level is not one this country uses for it at all. Callers turn that into
    a score; nothing here decides anything on its own.
    """
    if admin_level is None:
        return None
    levels = levels_for(country_code, kind)
    try:
        return levels.index(int(admin_level))
    except (ValueError, TypeError):
        return None


def is_too_coarse(country_code, kind, admin_level):
    """Whether a level is plainly bigger than the size being asked for.

    Used as a veto rather than a penalty: a state cannot be somebody's district
    however well its name happens to match, and letting score arithmetic decide
    that is how a confident wrong answer gets produced.
    """
    if admin_level is None:
        return False
    try:
        level = int(admin_level)
    except (TypeError, ValueError):
        return False
    if level <= 4:
        # Country, supranational, state or province — with one deliberate
        # exception: a handful of countries really do administer their largest
        # cities at level 4, and Kazakhstan is why this whole table exists.
        return level not in levels_for(country_code, 'city')
    smallest_for_kind = min(levels_for(country_code, kind))
    return level < smallest_for_kind - 2
