"""The App Review demo: one reviewer account and the made-up people around it.

App Review asked (guideline 2.1(a)) for a pre-populated demo account. Almost
everything in Approx depends on who is standing near you, and a reviewer sits
in an office on another continent from every real user, so pre-populated rows
alone would show an empty map: friends drop off after a day without a report,
and strangers have to be checked in, fresh, and inside the reviewer's zone.

So the cast follows the reviewer. Each time a demo account reports a position,
`arrange_around` keeps every member of `CAST` that has an offset at that
offset from it — moving them only when the reviewer has gone somewhere else,
and otherwise just keeping their positions fresh and the strangers checked in.
Nothing here changes what the reviewer has to do: strangers still appear only
after the reviewer taps Check in, exactly as for everyone else.

The cast is invisible to real accounts and vice versa — see
`User.is_review_demo` and the filters in `users.visibility.visible_users` and
`swifts.visibility`. `manage.py seed_review_demo` creates all of it.
"""
import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import User

REVIEWER_USERNAME = 'appreview'


@dataclass(frozen=True)
class Member:
    username: str
    first_name: str
    #: friend / connected / stranger / requester — how the seed relates them
    #: to the reviewer.
    role: str
    bio: str
    #: Metres (north, east) from the reviewer, or None to keep off the map.
    offset: tuple | None
    #: Owns the zone music the reviewer walks into.
    plays_zone_music: bool = False


CAST = (
    Member('demo_amina', 'Amina', 'friend', 'Coffee, climbing and too many playlists.',
           (60, 40), plays_zone_music=True),
    Member('demo_daniyar', 'Daniyar', 'friend', 'Morning runs and board games.', (180, -120)),
    Member('demo_sofia', 'Sofia', 'friend', 'Photographer. Always looking for good light.', (-250, 200)),
    Member('demo_timur', 'Timur', 'connected', 'Said hi at the climbing gym.', (420, -260)),
    Member('demo_alina', 'Alina', 'stranger', 'New in town, into hiking and live music.', (300, 260)),
    Member('demo_arman', 'Arman', 'stranger', 'Weekend cyclist, weekday coder.', (-200, -330)),
    Member('demo_madina', 'Madina', 'requester', 'Sent you a friend request.', None),
)

#: Roles that are only on the reviewer's map while checked in.
CHECKED_IN_ROLES = frozenset({'connected', 'stranger'})

#: How far the reviewer may get from where the cast was last placed before it
#: is placed again. Small moves are left alone, so friends do not trail the
#: reviewer around the room.
REPLACE_DISTANCE_M = 750

METRES_PER_DEGREE = 111_320


def offset_position(lat, lon, north_m, east_m):
    lat, lon = float(lat), float(lon)
    new_lat = lat + north_m / METRES_PER_DEGREE
    new_lon = lon + east_m / (METRES_PER_DEGREE * math.cos(math.radians(lat)))
    q = Decimal('0.00000001')
    return Decimal(new_lat).quantize(q), Decimal(new_lon).quantize(q)


def _metres_between(a_lat, a_lon, b_lat, b_lon):
    from proximity.utils import haversine

    return haversine(float(a_lat), float(a_lon), float(b_lat), float(b_lon)) * 1000


def arrange_around(reviewer, now=None, anchor=None):
    """Keep the cast where the reviewer can find it. Returns the members moved.

    `anchor` is the reviewer's position when it is not the stored one: the map's
    private query carries a coordinate it never stores, and arranging from it
    means the demo does not hang on the phone's location report reaching us.
    """
    if not reviewer.is_review_demo:
        return []
    anchor_lat, anchor_lon = anchor or (reviewer.latitude, reviewer.longitude)
    if anchor_lat is None or anchor_lon is None:
        return []
    now = now or timezone.now()
    placed = {m.username: m for m in CAST if m.offset is not None}
    members = list(User.objects.filter(is_review_demo=True, username__in=placed).exclude(pk=reviewer.pk))
    if not members:
        return []

    def expected(member):
        return offset_position(anchor_lat, anchor_lon, *placed[member.username].offset)

    replace = any(
        m.latitude is None or m.longitude is None
        or _metres_between(m.latitude, m.longitude, *expected(m)) > REPLACE_DISTANCE_M
        for m in members
    )

    # An accuracy too: the zone music alert refuses a position without one.
    fields = {'location_updated_at', 'location_accuracy_m', 'ghost_mode'}
    for member in members:
        member.location_updated_at = now
        member.location_accuracy_m = 5.0
        member.ghost_mode = False
        if replace:
            member.latitude, member.longitude = expected(member)
            fields |= {'latitude', 'longitude'}
        if placed[member.username].role in CHECKED_IN_ROLES and not member.is_checked_in(now=now):
            fields |= set(member.start_check_in())
    User.objects.bulk_update(members, sorted(fields))

    _keep_content_current(reviewer, members, now, moved=replace, anchor=(anchor_lat, anchor_lon))
    return members if replace else []


def _keep_content_current(reviewer, members, now, moved, anchor):
    """Stories expire and meetups pass; the demo's should not."""
    from clubs.models import ClubEvent
    from stories.models import Story

    Story.objects.filter(user__in=members, expires_at__lt=now + timedelta(hours=2)).update(
        created_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=23),
    )
    events = ClubEvent.objects.filter(club__members__user=reviewer, created_by__is_review_demo=True).distinct()
    for event in events:
        update = {}
        if event.event_datetime < now + timedelta(hours=12):
            update['event_datetime'] = now + timedelta(days=2)
            update['expires_at'] = now + timedelta(days=2, hours=3)
        if moved or event.latitude is None:
            update['latitude'], update['longitude'] = offset_position(*anchor, 120, 220)
        if update:
            ClubEvent.objects.filter(pk=event.pk).update(**update)
