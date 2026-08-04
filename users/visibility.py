"""The single place where "can A see B, and which of B's fields" is decided.

Every endpoint that returns another user's data starts from `visible_users()`
and asks `can_view()` before emitting a field. Blocking, private accounts,
paused location sharing and the per-field audiences are all applied here, so a
new endpoint cannot accidentally skip one of them by forgetting a filter.

Two rules the callers rely on:
  * A hidden field is *absent* from the response, never present-and-empty.
  * A user you cannot see is indistinguishable from a user who does not exist —
    same 404, same body. Use `not_found()`.
"""
import hashlib
import math
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

# ─── Relationship tiers ───────────────────────────────────────────────────────
SELF = 'self'
FRIEND = 'friend'
CONNECTED = 'connected'
STRANGER = 'stranger'
BLOCKED = 'blocked'

#: Per-field visibility setting backing each logical field name.
FIELD_SETTINGS = {
    'interests': 'visibility_interests',
    'stories': 'visibility_stories',
    'stats': 'visibility_stats',
    'zone_audio': 'visibility_zone_audio',
    'connections': 'visibility_connections',
    'links': 'visibility_links',
    'presence': 'visibility_presence',
}


# ─── Blocking ─────────────────────────────────────────────────────────────────
def blocked_ids_for(user):
    """Every user id involved in a block with `user`, in either direction.

    Blocking is mutually invisible: the blocked user must not be able to tell
    they were blocked, so both sides are filtered identically.
    """
    from friends.models import BlockedUser

    return set(
        BlockedUser.objects.filter(blocker=user).values_list('blocked_id', flat=True)
    ) | set(
        BlockedUser.objects.filter(blocked=user).values_list('blocker_id', flat=True)
    )


# ─── Relationships ────────────────────────────────────────────────────────────
def relationship_map(viewer, target_ids):
    """{user_id: tier} for the given ids, in a fixed number of queries."""
    from friends.models import BlockedUser, FriendRequest, HiRequest

    target_ids = set(target_ids)
    if not target_ids:
        return {}

    result = {tid: STRANGER for tid in target_ids}
    result[viewer.pk] = SELF

    for s, r in HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED,
    ).filter(
        Q(sender=viewer, receiver_id__in=target_ids)
        | Q(receiver=viewer, sender_id__in=target_ids)
    ).values_list('sender_id', 'receiver_id'):
        other = r if s == viewer.pk else s
        result[other] = CONNECTED

    # Friendship outranks a plain connection, so it is applied second.
    for s, r in FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED,
    ).filter(
        Q(sender=viewer, receiver_id__in=target_ids)
        | Q(receiver=viewer, sender_id__in=target_ids)
    ).values_list('sender_id', 'receiver_id'):
        other = r if s == viewer.pk else s
        result[other] = FRIEND

    # A block overrides everything above it.
    for blocker, blocked in BlockedUser.objects.filter(
        Q(blocker=viewer, blocked_id__in=target_ids)
        | Q(blocked=viewer, blocker_id__in=target_ids)
    ).values_list('blocker_id', 'blocked_id'):
        other = blocked if blocker == viewer.pk else blocker
        result[other] = BLOCKED

    return result


def relationship(viewer, target):
    target_id = getattr(target, 'pk', target)
    if viewer.pk == target_id:
        return SELF
    return relationship_map(viewer, [target_id]).get(target_id, STRANGER)


def is_approved_viewer(rel):
    """Whether a private account opens up to someone at this tier."""
    return rel in (SELF, FRIEND, CONNECTED)


# ─── Queryset filter ──────────────────────────────────────────────────────────
def visible_users(viewer, queryset=None, *, exclude_self=True,
                  on_map=False, discoverable_by=None):
    """Base queryset of users `viewer` is allowed to receive at all.

    Always drops both directions of a block. The keyword flags add the
    surface-specific rules:

    on_map          — drop anyone who paused location sharing (proximity only)
    discoverable_by — 'username' or 'interest'; drop anyone opted out of that
                      search surface
    """
    from .models import User

    qs = User.objects.all() if queryset is None else queryset
    qs = qs.exclude(pk__in=blocked_ids_for(viewer))
    if exclude_self:
        qs = qs.exclude(pk=viewer.pk)
    if on_map:
        qs = qs.exclude(location_sharing=User.LocationSharing.PAUSED)
    if discoverable_by == 'username':
        qs = qs.filter(discoverable_by_username=True)
    elif discoverable_by == 'interest':
        qs = qs.filter(discoverable_by_interest=True)
    elif discoverable_by is not None:
        raise ValueError(f'unknown discovery surface: {discoverable_by!r}')
    return qs


# ─── Field-level checks ───────────────────────────────────────────────────────
def can_view(viewer, target, field, rel=None):
    """Whether `viewer` may receive `field` of `target`.

    Order matters: block, then the private-account gate, then the field's own
    audience. A private account hides every gated field from non-approved
    viewers regardless of how permissive the individual settings are.
    """
    from .models import User

    if rel is None:
        rel = relationship(viewer, target)
    if rel == SELF:
        return True
    if rel == BLOCKED:
        return False
    if target.is_private and not is_approved_viewer(rel):
        return False

    audience = getattr(target, FIELD_SETTINGS[field])
    if audience == User.Audience.NOBODY:
        return False
    # A music zone is never for a stranger, whatever this is set to — "Friends"
    # and "Everyone" collapse to the same audience here (see app/zone.tsx and
    # app/privacy.tsx, which no longer offer "Everyone" as a distinct choice).
    # Same shape as `motion_for`, which caps presence-derived motion the same
    # way regardless of the presence audience setting.
    if field == 'zone_audio':
        return rel in (FRIEND, CONNECTED)
    if audience == User.Audience.FRIENDS:
        return rel == FRIEND
    return True


def approved_ids_for(viewer):
    """(friend ids, approved ids) for `viewer`.

    "Approved" is friends plus accepted Hi connections — the tier that opens a
    private account. Blocked pairs are stripped from both sets.
    """
    from friends.models import FriendRequest, HiRequest

    blocked = blocked_ids_for(viewer)

    friend_ids = set()
    for s, r in FriendRequest.objects.filter(
        status=FriendRequest.Status.ACCEPTED,
    ).filter(Q(sender=viewer) | Q(receiver=viewer)).values_list('sender_id', 'receiver_id'):
        friend_ids.add(r if s == viewer.pk else s)

    approved_ids = set(friend_ids)
    for s, r in HiRequest.objects.filter(
        status=HiRequest.Status.ACCEPTED,
    ).filter(Q(sender=viewer) | Q(receiver=viewer)).values_list('sender_id', 'receiver_id'):
        approved_ids.add(r if s == viewer.pk else s)

    return friend_ids - blocked, approved_ids - blocked


def visible_field_q(viewer, field, prefix='user'):
    """Q selecting rows whose owner exposes `field` to `viewer`.

    The SQL counterpart of `can_view`, for list endpoints that must filter on
    somebody else's setting in the database instead of looping in Python.
    `prefix` is the path from the row to its owning user.
    """
    from .models import User

    setting = f'{prefix}__{FIELD_SETTINGS[field]}'
    owner = f'{prefix}_id'
    friend_ids, approved_ids = approved_ids_for(viewer)

    q = Q(**{owner: viewer.pk})
    q |= Q(**{setting: User.Audience.EVERYONE}) & (
        Q(**{f'{prefix}__is_private': False}) | Q(**{f'{owner}__in': approved_ids})
    )
    # Friends are always approved, so the private gate is already satisfied.
    q |= Q(**{setting: User.Audience.FRIENDS, f'{owner}__in': friend_ids})

    # Blocks are excluded here rather than left to the caller. Callers today
    # all happen to filter them separately, but this Q has to be safe on its
    # own or the next endpoint to use it inherits a leak.
    blocked = blocked_ids_for(viewer)
    if blocked:
        q &= ~Q(**{f'{owner}__in': blocked})
    return q


def can_surface_in_proximity(viewer, target, rel=None):
    """Whether `target` may be shown to `viewer` as a proximity result.

    Separate from `visible_users(on_map=True)` because the proximity surfaces
    that outlive the moment — the encounter log, nearby notifications — are
    read long after the row was written. A block or an opt-out that happened
    since must still apply, so those lists filter on read rather than trusting
    what was recorded.
    """
    from .models import User

    if rel is None:
        rel = relationship(viewer, target)
    if rel == BLOCKED:
        return False
    if target.location_sharing == User.LocationSharing.PAUSED:
        return False
    if rel in (SELF, FRIEND, CONNECTED):
        return True
    # Strangers only ever found each other through shared interests.
    return bool(target.discoverable_by_interest)


def proximity_visible_ids(viewer, candidate_ids):
    """The subset of `candidate_ids` that may surface to `viewer`, in two
    queries regardless of list length."""
    from .models import User

    candidate_ids = {cid for cid in candidate_ids if cid is not None} - {viewer.pk}
    if not candidate_ids:
        return set()

    rels = relationship_map(viewer, candidate_ids)
    allowed = set()
    for pk, sharing, discoverable in User.objects.filter(
        pk__in=candidate_ids,
    ).values_list('pk', 'location_sharing', 'discoverable_by_interest'):
        rel = rels.get(pk, STRANGER)
        if rel == BLOCKED or sharing == User.LocationSharing.PAUSED:
            continue
        if rel in (FRIEND, CONNECTED) or discoverable:
            allowed.add(pk)
    return allowed


def minimal_only(viewer, target, rel=None):
    """True when `target` is private and `viewer` has not been approved — the
    caller should emit username + avatar and nothing else."""
    if rel is None:
        rel = relationship(viewer, target)
    return bool(target.is_private) and not is_approved_viewer(rel)


# ─── Presence ─────────────────────────────────────────────────────────────────
#: How long after the last foreground beat a user still counts as online.
#: The app beats about every 5s (the nav bar's badge poll runs on every
#: authenticated screen), so this tolerates ~24 dropped beats before someone
#: on a bad connection is called offline.
ONLINE_WINDOW = timedelta(minutes=2)

#: Motion older than this is discarded rather than shown. A stale "fast" would
#: leave a trail behind someone who parked ten minutes ago.
MOTION_WINDOW = timedelta(seconds=90)


def is_online(target, now=None):
    if target.last_seen is None:
        return False
    return (now or timezone.now()) - target.last_seen <= ONLINE_WINDOW


def presence_for(viewer, target, rel=None, now=None):
    """`{'is_online': ..., 'last_seen': ...}`, or None when it must not be shown.

    None means the caller omits the key entirely. That distinction is the whole
    point: rendering a grey "offline" dot for someone who is hiding would both
    state something false and make hiding detectable, since they would read as
    permanently offline. An absent key renders as no indicator at all.
    """
    from .models import User

    if rel is None:
        rel = relationship(viewer, target)
    # Paused is the old Invisible: off the map, and not broadcasting that they
    # are around either.
    if target.location_sharing == User.LocationSharing.PAUSED and rel != SELF:
        return None
    if not can_view(viewer, target, 'presence', rel):
        return None

    now = now or timezone.now()
    online = is_online(target, now)
    return {
        'is_online': online,
        # Only useful once they are gone, and withheld while online so the
        # payload cannot be used to track session boundaries minute by minute.
        'last_seen': None if online or target.last_seen is None else target.last_seen.isoformat(),
    }


def motion_for(viewer, target, rel=None, now=None):
    """Coarse motion class, or None when it must not be shown.

    Strictly narrower than presence, on three counts:

    * Approved tier only. "Someone nearby is online" is ambient; "that stranger
      is in a vehicle heading that way" is a targeting primitive, so this never
      reaches strangers regardless of the presence audience.
    * Never while approximate. The position is snapped to a ~500m grid; a
      directional trail across those cells would partly undo the fuzzing.
    * Never when stale, and never when presence itself is hidden.
    """
    from .models import User

    if rel is None:
        rel = relationship(viewer, target)
    if rel not in (SELF, FRIEND, CONNECTED):
        return None
    if target.location_sharing == User.LocationSharing.APPROXIMATE and rel != SELF:
        return None
    if presence_for(viewer, target, rel, now) is None:
        return None
    if target.motion_updated_at is None:
        return None
    if (now or timezone.now()) - target.motion_updated_at > MOTION_WINDOW:
        return None
    return target.motion_class


# ─── Approximate location ─────────────────────────────────────────────────────
#: Side of the grid cell a fuzzed position is snapped to, in metres.
APPROX_GRID_M = 500


def snap_coordinates(user, lat, lon):
    """Snap a position to a ~500m grid, deterministically.

    Deterministic on purpose. Random jitter per request would let an observer
    average many samples back down to the true point; snapping to a fixed cell
    means every reading inside that cell returns the identical coordinate, so
    repeated observation reveals nothing further. The per-user offset stops the
    grid from being a global lattice whose cell centres are public knowledge.
    """
    lat = float(lat)
    lon = float(lon)

    digest = hashlib.sha256(f'aura-approx:{user.pk}'.encode()).digest()

    lat_step = APPROX_GRID_M / 111_320.0
    lat_off = (digest[0] / 255.0) * lat_step
    snapped_lat = math.floor((lat - lat_off) / lat_step) * lat_step + lat_off + lat_step / 2

    # Longitude spacing is derived from the *snapped* latitude, not the raw
    # one. Using the raw value would make the longitude grid drift as the user
    # moved north or south, so the fuzzed point would wobble inside its cell
    # and repeated samples would average back towards the truth.
    lon_step = APPROX_GRID_M / (111_320.0 * max(math.cos(math.radians(snapped_lat)), 0.01))
    lon_off = (digest[1] / 255.0) * lon_step
    snapped_lon = math.floor((lon - lon_off) / lon_step) * lon_step + lon_off + lon_step / 2

    return (
        Decimal(f'{max(-90.0, min(90.0, snapped_lat)):.8f}'),
        Decimal(f'{max(-180.0, min(180.0, snapped_lon)):.8f}'),
    )


def position_tolerance_m(user):
    """How far the position we store for `user` may be from where they stand.

    Zero for a precise sharer: what we hold is what their phone sent. For an
    Approximate sharer it is half the diagonal of a grid cell, which is the
    worst case for a point snapped to that cell's centre.

    The error is ours and deliberate, so anything comparing a stored position
    against a real-world boundary has to account for it rather than treat it as
    truth. Setting a region zone is the case that found this: the fuzzed point
    lands over a district border and the user is told they are not standing
    where they are standing.
    """
    from .models import User

    if user.location_sharing == User.LocationSharing.APPROXIMATE:
        # Half the diagonal of a square cell: sqrt(2)/2, rounded up a hair.
        return APPROX_GRID_M * 0.708
    return 0.0


def store_coordinates(user, lat, lon):
    """Apply the user's location-sharing mode and return what to persist.

    Approximate users never have a precise position written to the database —
    the fuzzing happens before storage, not on the way out, so a future
    endpoint that reads `user.latitude` directly cannot leak it.
    """
    from .models import User

    if user.location_sharing == User.LocationSharing.APPROXIMATE:
        return snap_coordinates(user, lat, lon)
    return Decimal(str(lat)), Decimal(str(lon))


# ─── Uniform "you cannot see this" ────────────────────────────────────────────
def not_found():
    """Identical to a genuine missing row, so absence and invisibility cannot
    be told apart."""
    return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
