"""Moving a denormalised counter without losing a concurrent write.

Lifted wholesale from the argument in `stories.views._adjust_likes`, which
exists because the obvious form was wrong in production:

    swift.like_count = swift.like_count + 1
    swift.save()

reads the count into Python and writes back read-value-plus-one. Two people
liking the same Swift at the same moment both read N and both write N+1, and
one of the likes is silently gone. A Swift in a feed is precisely where
simultaneous likes happen, so this is not a theoretical race.

The floor is applied in SQL for the same reason: a Python `max(0, ...)` would
be computed from the same stale read it is meant to protect against.
"""
from django.db.models import F, Value
from django.db.models.functions import Greatest

#: The columns this module is allowed to touch. A typo'd field name would
#: otherwise be a silent no-op — `.update()` on a filtered queryset raises for
#: an unknown field, but only once it reaches the database, and by then the
#: caller has already returned success to the client.
COUNTER_FIELDS = frozenset({
    'like_count', 'comment_count', 'repost_count', 'view_count',
})


def adjust(swift_id, field, delta):
    """Move `field` on one Swift by `delta`, in the database.

    Floored at zero, always. A counter that has drifted below the truth (a
    backfill, a hand-deleted row) must not be able to go negative and render as
    "-1 likes"; clamping is cheaper than a reconciliation job and honest enough
    at this scale.

    Takes an id rather than an instance on purpose — the caller must not be
    tempted to read the new value off a model it is holding, because the value
    it holds is exactly the stale read this function exists to avoid. Re-read
    the row if the count is needed.
    """
    from .models import SwiftVideo

    if field not in COUNTER_FIELDS:
        raise ValueError(f'not a counter field: {field!r}')
    if not delta:
        return

    value = F(field) + delta if delta > 0 else Greatest(F(field) + delta, Value(0))
    SwiftVideo.objects.filter(pk=swift_id).update(**{field: value})
