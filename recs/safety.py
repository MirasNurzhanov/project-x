"""What the viewer is permitted to see, applied once, over everything.

This is the stage X calls visibility filtering, and the single most important
property of this whole package is that it is **not** distributed across the
candidate sources. Sources propose; this disposes. A source that filtered for
itself would be a seventh place for the rule to be wrong, and the seventh place
is the one that leaks.

## It delegates rather than reimplements

Every rule here already exists in `swifts/visibility.py`, which is the module
the ordinary feed, the detail endpoint and the reveal endpoint all go through.
This calls it. A second implementation of "may this person watch this" that
happened to agree today would disagree the first time either changed, and the
disagreement would be silent — the recommender would simply start showing rows
the rest of the app hides.

So the filter is one `watchable_q` applied to the candidate id set, and that Q
is the same object `SwiftFeedView` uses. Deleted, removed, still-processing,
out-of-audience, blocked-either-way: all of it comes from there for free, and
all of it stays correct when the gate changes.

## Location is not a ranking input, and cannot become one

The recommender never reads `latitude`, `longitude` or `place_name`. Not to
filter, not to score, not to diversify. `location_shared` — a boolean about
whether a reveal button exists — is the only location-adjacent column any of
this touches, and it is used solely as a weak content-type feature.

That is not a convention, it is enforced: `tests/test_privacy.py` asserts the
queryset this module builds has no coordinate column deferred *or* loaded, and
that no module under `recs/` references the field names at all. A recommender
that needed a hidden coordinate to rank a Swift would have defeated
`swifts/visibility.py` without touching it.
"""
from __future__ import annotations

from swifts import visibility as swift_visibility
from swifts.models import SwiftVideo

#: Column names no module in this package may read. Asserted by the privacy
#: test, and listed here so the assertion has one home.
FORBIDDEN_COLUMNS = ('latitude', 'longitude', 'place_name')


def permitted_ids(viewer, swift_ids):
    """Of `swift_ids`, the ones `viewer` may actually watch.

    One query, whatever the candidate count. Returns a set because every caller
    wants membership rather than order — order belongs to the ranker.
    """
    if not swift_ids:
        return set()
    return set(
        SwiftVideo.objects
        .filter(swift_visibility.watchable_q(viewer))
        .filter(id__in=list(swift_ids))
        .exclude(author_id=viewer.pk)
        .values_list('id', flat=True)
    )


def apply(viewer, candidates):
    """Drop every candidate the viewer may not watch.

    Returns `(kept, removed_count)`. The count is returned rather than logged
    here because the caller owns the metric namespace, and because a filter
    that logs is a filter that cannot be used inside a benchmark.
    """
    if not candidates:
        return [], 0
    allowed = permitted_ids(viewer, [c.swift_id for c in candidates])
    kept = [c for c in candidates if c.swift_id in allowed]
    return kept, len(candidates) - len(kept)


def suppressed_creator_ids(viewer):
    """Creators this viewer has told us to stop showing them.

    Distinct from a block, which `swift_visibility.watchable_q` already
    handles: this is "not interested", which is quieter and does not sever the
    relationship. It belongs here rather than in scoring because it is a
    filter, not a penalty — a viewer who said no should get zero, not fewer.
    """
    from .models import CreatorAffinity

    return set(
        CreatorAffinity.objects
        .filter(viewer=viewer, suppressed=True)
        .values_list('creator_id', flat=True)
    )
