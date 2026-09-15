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

#: Fields whose "Everyone" is not literal: the audience is capped at friends
#: however the setting reads. `can_view` is where each cap is argued; this is
#: the list `visible_field_q` needs to apply the same thing in SQL, and the two
#: must not drift.
#:
#: `zone_audio` is capped at friends-or-connected rather than friends, so it is
#: not on this list - it has no queryset filter and `can_view` is its only
#: gate.
FRIENDS_ONLY_FIELDS = frozenset({'stories'})

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


# ─── On the map ───────────────────────────────────────────────────────────────
# Being on the map is a choice, and `User.location_sharing` is where it is
# recorded. Nothing else — not an OS permission, not a location report, not
# having the app open — puts a person on another user's screen; the field
# defaults to PAUSED and only a deliberate press moves it off.
#
# That choice is necessary but not sufficient, which is why there are two
# predicates here rather than one:
#
#   sharing_q / is_sharing   the choice alone. "Has this person asked to be
#                            visible." The write path and every self-anchored
#                            query use this, and they must: gating a location
#                            *write* on the freshness of the position it is
#                            about to replace is a deadlock — a user whose fix
#                            went stale could never post a fresh one.
#
#   on_map_q / is_on_map     the choice AND a position recent enough to stand
#                            behind. "May this person be drawn on somebody
#                            else's screen, right now." Everything that
#                            publishes a coordinate uses this.
#
# The second exists because `location_sharing` has no expiry by design (see
# users/0030 and the sharing-session note on the model): a phone that is
# force-killed, loses signal for good, or simply stops reporting leaves the
# field non-paused and the last coordinate sitting in the row. Without a
# freshness gate that row keeps being served as though the person were standing
# there — which is the one thing a live map must never claim. The session
# teardown catches it on the next launch, and "the next launch" may be never.
#
# Each pair is a Python half and a SQL half, because list endpoints have to
# filter in the database and per-row callers cannot. Within a pair, neither
# half may grow a condition the other lacks.

#: How old the position we hold may be before we stop acting on it.
#:
#: Fifteen minutes, and this is the app's single answer to "how old is too old".
#: `clubs.visibility` reads it from here rather than keeping its own, so a
#: position cannot be too stale to draw on a map yet fresh enough to decide
#: which city someone is in.
#:
#: Fifteen minutes is what an actively-shared position looks like with room to
#: spare: the foreground map reports every 10s and the iOS background task every
#: 30s or 50m, so this tolerates roughly thirty consecutive missed updates —
#: a tunnel, a dead spot, a spell of no signal — before it gives up.
#:
#: It has a real consequence on Android and that consequence is correct. Android
#: ships with `isAndroidBackgroundLocationEnabled: false`, so positions there
#: stop the moment the app leaves the foreground; an Android user who backgrounds
#: Approx drops off other people's maps about fifteen minutes later even though
#: they are still checked in. That is the honest outcome — we genuinely do not
#: know where they are — and `is_sharing` keeps them able to resume instantly by
#: reopening the app, without touching the setting.
DEFAULT_POSITION_MAX_AGE_S = 15 * 60


def position_max_age_s():
    from django.conf import settings

    return getattr(settings, 'MAP_POSITION_MAX_AGE_S', DEFAULT_POSITION_MAX_AGE_S)


def sharing_q(prefix=None):
    """Q selecting rows whose owner may appear on somebody else's map.

    The choice alone — says nothing about whether we currently hold a position
    for them. `prefix` is the path from the row to the user, for tables that
    point at one (`'user'`, `'author'`); None when filtering users directly.
    """
    base = '' if prefix is None else f'{prefix}__'
    return Q(**{f'{base}ghost_mode': False})


def fresh_position_q(prefix=None, now=None):
    """Q selecting rows whose stored position is recent enough to publish.

    Requires a position to exist before asking how old it is. That was
    redundant while sharing defaulted to off — such a row was never reached —
    but sharing defaults to on now, and the `updated_at` fallback below ticks
    for edits that have nothing to do with location, so a row with no
    coordinate would otherwise read as freshly located because it was freshly
    saved. Must agree with `User.has_fresh_position`, which carries the same
    guard for the same reason.

    `location_updated_at` is the honest answer. `updated_at` is the fallback for
    rows written before that column existed (users/0018) — an upper bound on the
    age rather than the age, which is the right direction to be wrong in: it can
    only make a position look *newer* than it is, and the owner's next report
    corrects it. Same reasoning, and same fallback, as
    `clubs.visibility._position_age`.
    """
    base = '' if prefix is None else f'{prefix}__'
    cutoff = (now or timezone.now()) - timedelta(seconds=position_max_age_s())
    has_point = Q(**{
        f'{base}latitude__isnull': False,
        f'{base}longitude__isnull': False,
    })
    recent = Q(**{f'{base}location_updated_at__gte': cutoff}) | Q(**{
        f'{base}location_updated_at__isnull': True,
        f'{base}updated_at__gte': cutoff,
    })
    return has_point & recent


def on_map_q(prefix=None, now=None):
    """Q selecting rows that may be drawn on somebody else's map right now.

    The choice *and* a position we are still willing to stand behind. The SQL
    counterpart of `User.is_on_map`.
    """
    return sharing_q(prefix) & fresh_position_q(prefix, now)


# ─── Queryset filter ──────────────────────────────────────────────────────────
def visible_users(viewer, queryset=None, *, exclude_self=True,
                  on_map=False, discoverable_by=None):
    """Base queryset of users `viewer` is allowed to receive at all.

    Always drops both directions of a block. The keyword flags add the
    surface-specific rules:

    on_map          — drop anyone not currently on the map, and — because Ghost
                      Mode is mutual — return nobody at all when the *viewer*
                      is ghosted
    discoverable_by — 'username' or 'interest'; drop anyone opted out of that
                      search surface

    `on_map` is the single gate every surface that publishes a position already
    passed, so the rule lives in one place and no new endpoint can reach a
    position without opting into it.

    Mutuality is enforced here rather than at each map endpoint for exactly that
    reason: a ghost who could still read the map would be opting out of being
    seen while keeping the benefit of seeing, and putting the check anywhere
    else means remembering it at every new caller.
    """
    from .models import User

    qs = User.objects.all() if queryset is None else queryset
    qs = qs.filter(is_active=True, deletion_requested_at__isnull=True, retired_account_id__isnull=True)
    qs = qs.exclude(pk__in=blocked_ids_for(viewer))
    if exclude_self:
        qs = qs.exclude(pk=viewer.pk)
    if on_map:
        if getattr(viewer, 'ghost_mode', False):
            return qs.none()
        qs = qs.filter(on_map_q())
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

    Order matters: block, anonymous profile, private account, field audience.
    Anonymous profiles hide their details even from approved friends. Owners
    retain access to their own data.
    """
    from .models import User

    if not target.is_active or target.deletion_requested_at or target.retired_account_id:
        return False
    if rel is None:
        rel = relationship(viewer, target)
    if rel == SELF:
        return True
    if rel == BLOCKED:
        return False
    if target.ghost_mode:
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
    # And a story is never for a stranger either - same collapse, one tier
    # tighter. A story is a photograph of where somebody is and who they are
    # with, and the product it belongs to is "show this to your friends", not
    # "publish it to whoever happens to be standing nearby". So "Everyone"
    # means the same thing as "Friends" here, and "Nobody" still means nobody.
    #
    # Not CONNECTED, unlike zone audio: a Hi is a hello, and the audience for
    # a story is the list of people who were actually chosen. Whether a story
    # that passes this may also be *placed on a map* is a second question,
    # asked separately - see stories/visibility.py.
    if field == 'stories':
        return rel == FRIEND
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
    if field in FRIENDS_ONLY_FIELDS:
        # The SQL half of the cap in `can_view`: "Everyone" is not literal for
        # these, so both settings resolve to the same audience and only
        # "Nobody" differs. The private-account gate needs no clause of its
        # own here - a friend is always an approved viewer.
        q |= ~Q(**{setting: User.Audience.NOBODY}) & Q(**{f'{owner}__in': friend_ids})
    else:
        q |= Q(**{setting: User.Audience.EVERYONE}) & (
            Q(**{f'{prefix}__is_private': False}) | Q(**{f'{owner}__in': approved_ids})
        )
        # Friends are always approved, so the private gate is already satisfied.
        q |= Q(**{setting: User.Audience.FRIENDS, f'{owner}__in': friend_ids})

    # Anonymous profiles retract existing content as well as new posts. Keep
    # the owner exception in SQL so list endpoints agree with `can_view`.
    q &= Q(**{owner: viewer.pk}) | Q(**{f'{prefix}__ghost_mode': False})

    # Blocks are excluded here rather than left to the caller. Callers today
    # all happen to filter them separately, but this Q has to be safe on its
    # own or the next endpoint to use it inherits a leak.
    blocked = blocked_ids_for(viewer)
    if blocked:
        q &= ~Q(**{f'{owner}__in': blocked})
    return q & Q(**{f'{prefix}__is_active': True,
                   f'{prefix}__deletion_requested_at__isnull': True,
                   f'{prefix}__retired_account_id__isnull': True})


def can_surface_in_proximity(viewer, target, rel=None):
    """Whether `target` may be shown to `viewer` as a proximity result.

    Separate from `visible_users(on_map=True)` because the proximity surfaces
    that outlive the moment — the encounter log, nearby notifications — are
    read long after the row was written. A block or an opt-out that happened
    since must still apply, so those lists filter on read rather than trusting
    what was recorded.
    """
    from .models import User

    # Ghost Mode is mutual even for notifications and encounter records that
    # predate the switch. A live target cannot make a hidden viewer eligible.
    if getattr(viewer, 'ghost_mode', False):
        return False
    if rel is None:
        rel = relationship(viewer, target)
    if rel == BLOCKED:
        return False
    # "Someone is near you" is a publication of where `target` is standing, so
    # it takes the same consent the map dot does. Applied per direction by the
    # callers, which ask this twice with the pair swapped.
    if not target.is_on_map():
        return False
    if rel in (SELF, FRIEND, CONNECTED):
        return True
    # Strangers only ever found each other through shared interests.
    return bool(target.discoverable_by_interest)


def proximity_visible_ids(viewer, candidate_ids):
    """The subset of `candidate_ids` that may surface to `viewer`, in two
    queries regardless of list length."""
    from .models import User

    if getattr(viewer, 'ghost_mode', False):
        return set()
    candidate_ids = {cid for cid in candidate_ids if cid is not None} - {viewer.pk}
    if not candidate_ids:
        return set()

    rels = relationship_map(viewer, candidate_ids)
    allowed = set()
    for pk, ghosted, discoverable in User.objects.filter(
        pk__in=candidate_ids,
    ).values_list('pk', 'ghost_mode', 'discoverable_by_interest'):
        rel = rels.get(pk, STRANGER)
        if rel == BLOCKED or ghosted:
            continue
        if rel in (FRIEND, CONNECTED) or discoverable:
            allowed.add(pk)
    return allowed


# Note this one *does* apply the live setting, unlike the encounter history it
# feeds: `location_sharing` is checked above, so pausing retracts you from the
# lists as well as from the map. That is the older behaviour and it is kept
# deliberately — pausing is the strongest thing a user can say on this subject,
# and it should not leave them legible in a second place.


def minimal_only(viewer, target, rel=None):
    """Whether only the basic profile identity may be shown.

    Ghost Mode hides profile details from everyone else, including friends.
    Ordinary private accounts still open to their approved viewers.
    """
    if rel is None:
        rel = relationship(viewer, target)
    if rel == SELF:
        return False
    if target.ghost_mode:
        return True
    return bool(target.is_private) and not is_approved_viewer(rel)


def can_view_profile_details(viewer, target, rel=None):
    """Shared account-level gate for a profile and its Swift collection."""
    if rel is None:
        rel = relationship(viewer, target)
    return rel != BLOCKED and not minimal_only(viewer, target, rel)


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
    if rel is None:
        rel = relationship(viewer, target)
    # A ghost is off the map and not broadcasting that they are around either.
    if target.ghost_mode and rel != SELF:
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
    * Never to a viewer who is seeing a fuzzed position. Their pin is snapped
      to a ~500m grid, and a directional trail across those cells would partly
      undo the fuzzing. That used to mean "never while the target is on
      Approximate"; precision follows the relationship now, so it means
      friends and the user themselves — a Hi connection gets a cell, so it
      does not get a heading to go with it.
    * Never when stale, and never when presence itself is hidden.
    """
    if rel is None:
        rel = relationship(viewer, target)
    if rel not in (SELF, FRIEND):
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


def is_friend(viewer, target, friend_ids=None):
    """Whether `viewer` and `target` are friends, cheaply if possible.

    `friend_ids` is the set from `approved_ids_for(viewer)`. Pass it whenever
    more than one target is being judged for the same viewer — list endpoints
    must, or this is a query per row.
    """
    if viewer is None or getattr(viewer, 'pk', None) is None:
        return False
    if target.pk == viewer.pk:
        return True
    if friend_ids is None:
        friend_ids, _ = approved_ids_for(viewer)
    return target.pk in friend_ids


def coordinates_for(viewer, target, friend_ids=None):
    """The position `viewer` is allowed to see for `target`.

    **The one place a stored position becomes a published one.** Precision is
    not a setting any more: a friend — and the user themselves — gets the
    precise point, everybody else gets it snapped to a ~500m cell.

    This is the read-time half of an inversion. Fuzzing used to happen before
    storage, which meant a precise position for an Approximate user did not
    exist to leak; friends needing the real point ended that, so the database
    now holds the truth and *every* read has to come through here. A caller
    that reaches for `target.latitude` directly is the bug this function exists
    to make obvious.

    `snap_coordinates` is deterministic per user, so repeated reads of the same
    stored point return the identical cell. That is what makes fuzzing at read
    time safe: an observer sampling a stranger a thousand times cannot average
    the jitter away, because there is none.

    Returns `(None, None)` for a target with no stored position, so callers can
    treat "not shareable" and "not known" the same way.
    """
    if target.latitude is None or target.longitude is None:
        return None, None
    if is_friend(viewer, target, friend_ids):
        return target.latitude, target.longitude
    return snap_coordinates(target, target.latitude, target.longitude)


def position_tolerance_for(viewer, target, friend_ids=None):
    """How far the position `viewer` sees for `target` may be from the truth.

    Zero when they are entitled to the precise point; otherwise half the
    diagonal of a grid cell, which is the worst case for a point snapped to
    that cell's centre.

    The error is ours and deliberate, so anything comparing a *published*
    position against a real-world boundary has to account for it rather than
    treat it as truth.
    """
    if is_friend(viewer, target, friend_ids):
        return 0.0
    # Half the diagonal of a square cell: sqrt(2)/2, rounded up a hair.
    return APPROX_GRID_M * 0.708


def store_coordinates(user, lat, lon):
    """What to persist for `user`. Always the precise pair.

    Kept as a named function though it no longer transforms anything: it is the
    one write path, and having it makes "where does a position enter the
    database" answerable in one grep. The fuzzing that used to live here moved
    to `coordinates_for`, because friends need the real point and a row can
    only hold one version of it.
    """
    return Decimal(str(lat)), Decimal(str(lon))


# ─── Uniform "you cannot see this" ────────────────────────────────────────────
def not_found():
    """Identical to a genuine missing row, so absence and invisibility cannot
    be told apart."""
    return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
