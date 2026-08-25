"""Two different questions about one story, deliberately kept apart.

    may this viewer *open* this story?        can_read / readable_q
    may this viewer see it *on the map*?      can_show_on_map / placed_q

They used to be one question, and that was the bug. A story is two things at
once: something you show your friends, and — sometimes — a pin at a coordinate.
Answering both with the map's rule meant that turning location sharing off took
your stories away from your friends as well, which is not what the switch says
it does and not what it is for.

So the split, stated once here rather than re-derived per endpoint:

    reading a story    friendship, blocking, the author's Stories setting,
                       expiry. Location sharing is irrelevant — a paused
                       author's stories still reach their friends.

    placing a story    everything above, plus: the story actually has a
                       coordinate, and its author is on the map *right now*.

The second is checked on read, not at write time, because the row outlives the
moment it was written: pausing has to retract pins already posted, and resuming
has to bring them back. A write-time gate could do neither. It is the same
shape as `users.visibility.can_surface_in_proximity`, and for the same reason.

`viewer`'s own stories pass both. Their own archive and their own pin on their
own map are not a publication to anybody else.
"""
from django.db.models import Q

from moderation.reasons import HIDDEN_STATES
from users import visibility


# ─── Reading a story ──────────────────────────────────────────────────────────
def can_read(viewer, story):
    """Whether `viewer` may open `story` at all.

    Blocking, the private-account gate and the author's Stories audience, which
    `users.visibility` caps at friends however it is set. Expiry is the
    caller's to apply — the archive and the reaction endpoint want different
    answers about an expired story.
    """
    if story.moderation in HIDDEN_STATES and story.user_id != viewer.pk:
        return False
    return visibility.can_view(viewer, story.user, 'stories')


def readable_q(viewer, prefix='user'):
    """The SQL half of `can_read`. `prefix` is the path from the row to the
    story's author (`'user'`, or `'story__user'` from a bookmark).

    The moderation clause is prefix-aware for the same reason the audience one
    is: read from a bookmark the row is a `SavedStory`, so the column lives at
    `story__moderation` rather than `moderation`.
    """
    owner = 'user_id' if prefix == 'user' else f'{prefix}_id'
    moderation_field = 'moderation' if prefix == 'user' else (
        prefix.rsplit('__', 1)[0] + '__moderation'
    )
    # Quarantined and removed stories are gone for everybody but their author,
    # who keeps seeing their own - the same rule comments take, and for the
    # same reason: content that vanishes without explanation reads as a bug.
    not_hidden = (
        ~Q(**{f'{moderation_field}__in': HIDDEN_STATES})
        | Q(**{owner: viewer.pk})
    )
    return visibility.visible_field_q(viewer, 'stories', prefix=prefix) & not_hidden


# ─── Placing a story on the map ───────────────────────────────────────────────
def is_placed_for(viewer, story):
    """The location half of `can_show_on_map`, on its own.

    For callers who have already established that `viewer` may read the story —
    a queryset filtered by `readable_q`, say — and only need to know whether
    its position may go out with it. Costs no queries: it reads two columns and
    the author's own setting, which every caller has select_related already.
    """
    if not story.is_placed:
        return False
    # `is_sharing`, not `is_on_map`. A pin sits at the coordinate the story was
    # posted at, so it makes no claim about where its author is standing now —
    # and withdrawing a friend's pin six hours after their phone stopped
    # reporting would hide a story that is still theirs to show. Freshness
    # gates the live user marker, which does make that claim.
    return story.user_id == viewer.pk or story.user.is_sharing()


def can_show_on_map(viewer, story):
    """Whether `viewer` may see `story` as a pin.

    Strictly narrower than `can_read`: a pin publishes a coordinate under the
    author's name and face, which is exactly what the map dot publishes and
    exactly what Paused withholds. So the author has to be on the map now — not
    when they posted — and the story has to have a position to draw.
    """
    return is_placed_for(viewer, story) and (
        story.user_id == viewer.pk or can_read(viewer, story)
    )


def placed_q(viewer):
    """The SQL half of `can_show_on_map`, minus expiry. Filters a Story
    queryset.

    Composed with `readable_q` rather than written flat: everything that keeps
    a story off the map is *additional* to what keeps it out of the feed, and
    one clause doing both jobs is how the two came to be confused for each
    other in the first place.
    """
    has_position = Q(latitude__isnull=False, longitude__isnull=False)
    # `sharing_q`, matching `is_placed_for` — see the note there for why a pin
    # is not gated on the freshness of its author's own position.
    author_sharing = Q(user_id=viewer.pk) | visibility.sharing_q('user')
    return readable_q(viewer) & has_position & author_sharing
