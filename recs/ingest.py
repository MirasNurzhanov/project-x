"""Turning what a viewer did into what the recommender knows.

One entry point, `record()`. It writes the append-only `SwiftSignal` row and
folds the same event into `CreatorAffinity` in the same transaction, so the
summary can never drift from the log it summarises.

## Why the server reinterprets what the client sends

The client reports `WATCH` with a duration. The server decides whether that was
a watch, a low watch or a skip, because the threshold is a tuning decision and
tuning decisions do not belong in an app build that takes a week to reach every
phone. `normalise()` is that reinterpretation and it is where the client's
vocabulary ends.

## Decay

Affinity is a decayed running total. Rather than a nightly job over every pair,
the decay is applied *on write*: the stored score is first aged forward from
`last_signal_at` to now, then the new signal is added. A pair that is never
touched again is therefore stale — which is why every read goes through
`decayed_score()` and ages it again for the read. Two applications of the same
half-life, never compounding, because the write also moves `last_signal_at`.
"""
from __future__ import annotations

import math

from django.db import transaction
from django.utils import timezone

from .models import CreatorAffinity, SwiftSignal
from .params import load_params


def _half_life_factor(elapsed_days: float, half_life_days: float) -> float:
    if elapsed_days <= 0 or half_life_days <= 0:
        return 1.0
    return math.pow(0.5, elapsed_days / half_life_days)


def decayed_score(affinity, *, now=None, half_life_days=None) -> float:
    """`affinity.score` aged forward to now.

    Every read of an affinity goes through this. Reading the raw column would
    treat a year-old obsession and yesterday's as the same number.
    """
    if half_life_days is None:
        half_life_days = load_params().limits.affinity_half_life_days
    now = now or timezone.now()
    elapsed_days = max(0.0, (now - affinity.last_signal_at).total_seconds() / 86400.0)
    return affinity.score * _half_life_factor(elapsed_days, half_life_days)


def normalise(kind, completion_pct):
    """What the client reported → what actually happened.

    A watch under the skip threshold is a skip; under the low-watch threshold
    it is a low watch. Both are negative signals, and neither is something the
    client should be trusted to classify, since the thresholds move.
    """
    if kind != SwiftSignal.Kind.WATCH:
        return kind
    if completion_pct < SwiftSignal.SKIP_COMPLETION_PCT:
        return SwiftSignal.Kind.SKIP
    if completion_pct < SwiftSignal.LOW_WATCH_COMPLETION_PCT:
        return SwiftSignal.Kind.LOW_WATCH
    return SwiftSignal.Kind.WATCH


#: Signals that make a creator sticky-suppressed for this viewer.
_SUPPRESSING = frozenset({
    SwiftSignal.Kind.NOT_INTERESTED,
    SwiftSignal.Kind.BLOCK_CREATOR,
    SwiftSignal.Kind.REPORT,
})

_NEGATIVE = frozenset({
    SwiftSignal.Kind.SKIP,
    SwiftSignal.Kind.LOW_WATCH,
    SwiftSignal.Kind.NOT_INTERESTED,
    SwiftSignal.Kind.REPORT,
    SwiftSignal.Kind.BLOCK_CREATOR,
})


@transaction.atomic
def record(
    viewer,
    kind,
    *,
    swift=None,
    creator=None,
    watch_ms=0,
    completion_pct=0,
    served_by='',
    position=None,
):
    """Write one signal and fold it into the affinity summary.

    `creator` is taken from `swift.author` when not given. A signal with
    neither is still recorded — it is a fact about the viewer — but it moves no
    affinity, because there is nobody to move it toward.

    Returns the `SwiftSignal` that was written.
    """
    completion_pct = max(0, min(100, int(completion_pct or 0)))
    kind = normalise(kind, completion_pct)

    if creator is None and swift is not None:
        creator = swift.author

    signal = SwiftSignal.objects.create(
        viewer=viewer,
        swift=swift,
        creator=creator,
        kind=kind,
        watch_ms=max(0, int(watch_ms or 0)),
        completion_pct=completion_pct,
        served_by=served_by or '',
        position=position,
    )

    if creator is not None and creator.pk != viewer.pk:
        _apply_to_affinity(viewer, creator, kind, completion_pct)

    return signal


def _apply_to_affinity(viewer, creator, kind, completion_pct):
    """Fold one signal into the (viewer, creator) summary.

    `select_for_update` rather than an `F()` expression: this reads the stored
    score, ages it, and writes a value derived from both — arithmetic a single
    UPDATE cannot express. The row is per-pair and the lock is held for
    microseconds, so the contention this would cause on a hot counter does not
    arise here.
    """
    params = load_params()
    now = timezone.now()

    affinity, created = CreatorAffinity.objects.select_for_update().get_or_create(
        viewer=viewer,
        creator=creator,
        defaults={'last_signal_at': now},
    )

    aged = affinity.score if created else decayed_score(
        affinity, now=now, half_life_days=params.limits.affinity_half_life_days,
    )
    affinity.score = aged + SwiftSignal.polarity_for(kind, completion_pct)
    affinity.last_signal_at = now

    if kind in _NEGATIVE:
        affinity.negative_count += 1
        if kind == SwiftSignal.Kind.SKIP:
            affinity.skip_streak += 1
    elif SwiftSignal.polarity_for(kind, completion_pct) > 0:
        affinity.positive_count += 1
        # Any genuine positive ends the run. A creator whose next Swift you
        # actually watched is not a creator you are skipping.
        affinity.skip_streak = 0

    if kind in _SUPPRESSING:
        affinity.suppressed = True

    affinity.save(update_fields=[
        'score', 'positive_count', 'negative_count',
        'skip_streak', 'suppressed', 'last_signal_at', 'updated_at',
    ])
    return affinity
