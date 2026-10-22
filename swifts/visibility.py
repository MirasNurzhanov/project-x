"""Two different questions about one Swift, deliberately kept apart.

    may this viewer *watch* it?              can_watch / watchable_q
    may this viewer *learn where it was*?    can_reveal_location

This is the same split `stories/visibility.py` makes, for the same reason and
with the same hard-won lesson behind it: a Swift is two things at once, a video
somebody posted and — sometimes — a place somebody stood. Answering both with
one rule means the answer is wrong for one of them.

    watching       the Swift is live, nobody has blocked anybody, and the
                   viewer is inside the audience the creator chose. Location is
                   irrelevant: a Swift with a location is watched by exactly
                   the same people as one without.

    revealing      everything above, **plus** three more things: the creator
                   turned location sharing on for this Swift, a coordinate
                   actually exists, and the viewer has genuinely watched it.

The third of those is the one this module exists to make unforgeable. It is
answered from `SwiftView` — a row this server wrote — and never from anything
the client asserts. A request that says `hasViewed: true` is a request that
says nothing.

One rule the callers rely on, inherited from `users.visibility`: a Swift you
cannot see is indistinguishable from a Swift that does not exist. Same 404,
same body, no exceptions — including for the reveal endpoint, where a 403 would
confirm that a Swift exists and has a location worth hiding.
"""
from django.db.models import Exists, OuterRef, Q

from users import visibility

#: Why a reveal was refused. The endpoint answers 404 for every one of these —
#: they are for logging, tests and the audit trail, never for the response body,
#: because telling somebody *which* condition they failed is telling them
#: something about a Swift they are not allowed to see.
DENY_NOT_FOUND = 'not_found'
DENY_NOT_SHARED = 'not_shared'
DENY_NO_COORDINATES = 'no_coordinates'
DENY_NOT_VIEWED = 'not_viewed'

ALLOW = None


# ─── Watching a Swift ─────────────────────────────────────────────────────────
def live_q():
    """Q selecting rows that are Swifts anybody could watch right now.

    The lifecycle half of the gate, with no viewer in it. SQL counterpart of
    `SwiftVideo.is_live`, and the two must never disagree.
    """
    from .models import SwiftVideo

    return (
        Q(deleted_at__isnull=True)
        & Q(status=SwiftVideo.Status.READY)
        # Publication requires explicit approval, including for future states.
        & Q(moderation=SwiftVideo.Moderation.OK)
    )


def _blocked_author_q(viewer):
    from friends.models import BlockedUser

    return Q(Exists(BlockedUser.objects.filter(
        Q(blocker_id=viewer.pk, blocked_id=OuterRef('author_id'))
        | Q(blocked_id=viewer.pk, blocker_id=OuterRef('author_id'))
    )))


def audience_q(viewer):
    """Q selecting rows whose creator meant them for `viewer`.

    Three audiences and account-level caps over the top of them. Ghost Mode
    hides every audience from other people, including approved friends:

      PRIVATE   the author, and nobody else. Not friends, not connections.
      FRIENDS   accepted friendships only. A Hi is a hello, not an audience —
                same line `users.visibility` draws for stories.
      PUBLIC    everyone, *unless the author's account is private*, in which
                case it reaches approved viewers only. Without that clause a
                private account would publish to the world by posting a Swift,
                which is not what the account switch says it does.

    The author's own rows always pass. Their own Swift on their own profile is
    not a publication to anybody.
    """
    return _audience_q(viewer, ~_blocked_author_q(viewer))


def _audience_q(viewer, unblocked):
    """`audience_q` with the block clause its approved-viewer branches use passed in.

    `watchable_q` already requires "nobody blocked anybody" for every row, so it
    passes an empty Q here: repeating the clause inside the audience would put
    the same correlated blocked-user subquery into the SQL three times without
    changing a single answer.
    """
    from friends.models import FriendRequest, HiRequest
    from .models import SwiftVideo

    # Keep the relationship checks in the Swift query. Building Python ID sets
    # first costs four extra round trips each time a feed reapplies this gate,
    # and that materialized graph can go stale before the rows are fetched.
    pair = (
        Q(sender_id=viewer.pk, receiver_id=OuterRef('author_id'))
        | Q(receiver_id=viewer.pk, sender_id=OuterRef('author_id'))
    )
    friend = Q(Exists(FriendRequest.objects.filter(
        pair, status=FriendRequest.Status.ACCEPTED,
    )))
    connected = Q(Exists(HiRequest.objects.filter(
        pair, status=HiRequest.Status.ACCEPTED,
    )))

    own = Q(author_id=viewer.pk)

    public = Q(visibility=SwiftVideo.Visibility.PUBLIC) & (
        Q(author__is_private=False) | ((friend | connected) & unblocked)
    )
    # Friends are always approved viewers, so the private-account gate is
    # already satisfied and needs no clause of its own here.
    friends = Q(visibility=SwiftVideo.Visibility.FRIENDS) & friend & unblocked

    # The App Review demo's Swifts reach demo accounts only, and real Swifts
    # reach real accounts only — see `User.is_review_demo`.
    same_world = Q(author__is_review_demo=getattr(viewer, 'is_review_demo', False))

    # Anonymous creators retract all their Swifts from other people,
    # including existing friends. Their own collection remains available.
    return (own | (same_world & Q(author__ghost_mode=False) & (public | friends))) & Q(
        author__is_active=True, author__deletion_requested_at__isnull=True,
        author__retired_account_id__isnull=True,
    )


def watchable_q(viewer):
    """Q selecting every Swift `viewer` is allowed to watch.

    Live, inside the audience, and neither party has blocked the other. Blocks
    are excluded here rather than left to the caller: callers today all happen
    to filter them anyway, but this Q has to be safe on its own or the next
    endpoint to use it inherits a leak. Same argument, and same placement, as
    `users.visibility.visible_field_q`.

    That one clause covers every row, so the audience is built without its own
    copies of it: `(own | (A & unblocked)) & unblocked` is `(own | A) & unblocked`.
    """
    return live_q() & _audience_q(viewer, Q()) & ~_blocked_author_q(viewer)


def watchable(viewer, queryset=None):
    """The base queryset every read endpoint starts from."""
    from .models import SwiftVideo

    qs = SwiftVideo.objects.all() if queryset is None else queryset
    return qs.filter(watchable_q(viewer))


def can_watch(viewer, swift):
    """Python counterpart of `watchable_q`, for a row already in hand.

    Resolves this author only, rather than fetching a viewer's whole graph.
    Used by single-object endpoints that already have the Swift in hand.
    """
    from .models import SwiftVideo

    if not swift.is_live:
        return False
    if not swift.author.is_active or swift.author.deletion_requested_at or swift.author.retired_account_id:
        return False
    if swift.author_id == viewer.pk:
        return True
    if swift.author.is_review_demo != getattr(viewer, 'is_review_demo', False):
        return False
    if swift.author.ghost_mode:
        return False

    rel = visibility.relationship(viewer, swift.author_id)
    if rel == visibility.BLOCKED:
        return False

    if swift.visibility == SwiftVideo.Visibility.PRIVATE:
        return False
    if swift.visibility == SwiftVideo.Visibility.FRIENDS:
        return rel == visibility.FRIEND
    # PUBLIC, capped by the author's account privacy.
    if swift.author.is_private and not visibility.is_approved_viewer(rel):
        return False
    return True


# ─── Revealing where a Swift was made ─────────────────────────────────────────
def has_viewed(viewer, swift):
    """Whether this server has recorded `viewer` watching `swift`.

    Reads `SwiftView`, which only `swifts.views.RecordSwiftViewView` writes and
    only for a Swift the viewer was allowed to watch at the time. This is the
    entire basis for the "genuinely encountered it" rule — there is deliberately
    no parameter, header or body field anywhere in the reveal path that a client
    could set to influence it.
    """
    from .models import SwiftView

    return SwiftView.objects.filter(swift=swift, viewer=viewer).exists()


def reveal_denial(viewer, swift):
    """Why `viewer` may not learn where `swift` was made, or `ALLOW`.

    Ordered so the cheapest and most general conditions come first, and so the
    answer never depends on a condition the caller has not yet established.

    The creator is exempt from two of the four checks and from neither of the
    others: their own location is theirs to see without having watched their
    own video, and a Swift they have stopped sharing is still one they can look
    at — turning sharing off is a statement about other people. What they
    cannot do is reveal a location that was never stored, because there is
    nothing to return.
    """
    if not can_watch(viewer, swift):
        return DENY_NOT_FOUND
    if not swift.has_location:
        return DENY_NO_COORDINATES

    if swift.author_id == viewer.pk:
        return ALLOW

    if viewer.ghost_mode:
        return DENY_NOT_SHARED

    if not swift.location_shared:
        return DENY_NOT_SHARED

    if not has_viewed(viewer, swift):
        return DENY_NOT_VIEWED

    return ALLOW


def can_reveal_location(viewer, swift):
    return reveal_denial(viewer, swift) is ALLOW


def location_available_for(viewer, swift):
    """What the feed publishes as `location_available`.

    **Not** "this Swift has a location". That boolean answers a question about
    the Swift; this one answers a question about the viewer, and the difference
    is a real leak. A flag derived from column nullity tells a viewer that a
    Swift is hiding something *from them specifically* — and since the flag is
    stable, comparing it across accounts turns the feed into an oracle for
    which creators have location sharing switched on. Nobody agreed to publish
    that.

    So this is the reveal gate minus the one condition the button exists to
    satisfy: everything is checked *except* whether they have watched it yet,
    because at the moment the feed renders they have not, and the whole point
    of the control is that pressing it works.
    """
    if not swift.location_shared or not swift.has_location:
        return False
    if swift.author_id == viewer.pk:
        return True
    if viewer.ghost_mode:
        return False
    return can_watch(viewer, swift)
