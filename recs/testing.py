"""Fixtures for the recommender suites.

Builds on `swifts.testing` rather than duplicating it — same users, same
Swifts, same throttle-clearing `TestCase` — and adds only what ranking needs:
a way to give a viewer a history, and a way to age a Swift.

## Determinism

Every helper takes an explicit `now` or an explicit age. The recommender is a
function of time in several places — recency decay, affinity half-life,
trending windows, the new-creator join window — and a suite that lets those
read the wall clock is a suite that fails at midnight, or on a slow machine, or
in a month when the fixtures age out of the trending window. Nothing here calls
`timezone.now()` without being told to.
"""
from datetime import timedelta

from django.utils import timezone

from swifts.models import SwiftVideo
from swifts.testing import (  # noqa: F401 - re-exported for the suites
    SwiftTestCase, befriend, block, connect, make_swift, make_user,
)

from .ingest import record
from .models import SwiftSignal


def age_swift(swift, *, hours=0, days=0):
    """Move a Swift's `created_at` into the past.

    `auto_now_add` means the column cannot be set at creation, so this is an
    UPDATE. Refreshes the in-memory instance too, because a test that ages a
    row and then reads `swift.created_at` off the stale object measures nothing.
    """
    when = timezone.now() - timedelta(hours=hours, days=days)
    SwiftVideo.objects.filter(pk=swift.pk).update(created_at=when)
    swift.created_at = when
    return swift


def engage(viewer, swift, *, kind=SwiftSignal.Kind.LIKE, times=1, completion_pct=100):
    """Give `viewer` a positive history with `swift`'s creator."""
    for _ in range(times):
        record(viewer, kind, swift=swift, completion_pct=completion_pct)
    return swift


def skip(viewer, swift, *, times=1):
    """Give `viewer` a negative history — a run of immediate bounces.

    Sent as a `WATCH` with a low completion rather than as a bare `SKIP`,
    because that is what the client actually reports and because it exercises
    `ingest.normalise`, which is where the reinterpretation lives.
    """
    for _ in range(times):
        record(viewer, SwiftSignal.Kind.WATCH, swift=swift, completion_pct=3)
    return swift


def watch(viewer, swift, *, completion_pct=90, watch_ms=9000):
    return record(
        viewer, SwiftSignal.Kind.WATCH, swift=swift,
        completion_pct=completion_pct, watch_ms=watch_ms,
    )


def bulk_swifts(author, count, *, hours_apart=1, **extra):
    """`count` Swifts by one author, spaced back through time.

    Spacing matters: created in the same instant they sort by id, and a test
    about recency that is really about id order is a test that passes for the
    wrong reason.
    """
    made = []
    for i in range(count):
        swift = make_swift(author, **extra)
        age_swift(swift, hours=i * hours_apart)
        made.append(swift)
    return made


def popular(swift, *, likes=0, comments=0, reposts=0, views=0):
    """Put engagement counters on a Swift without generating the rows.

    The counters are denormalised on `SwiftVideo` and maintained by
    `swifts/counters.py`; for ranking tests what matters is the number the
    ranker reads, and manufacturing thousands of `SwiftLike` rows to produce it
    would make the suite slow for no additional coverage.
    """
    SwiftVideo.objects.filter(pk=swift.pk).update(
        like_count=likes, comment_count=comments,
        repost_count=reposts, view_count=views,
    )
    swift.refresh_from_db()
    return swift
