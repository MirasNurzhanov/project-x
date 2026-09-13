"""What has already been shown, and when it may come back.

## Served is not seen

A page of ten Swifts is *served*. A viewer who swipes three times has *seen*
three. If serving counted as seeing, every page would burn seven candidates
nobody looked at, and a few sessions in, a viewer with a modest candidate pool
would find the feed empty for reasons no log would explain.

So exposure has two records with two different meanings, and both already
exist:

    `SwiftImpression`   we put this in a response.       Provisional.
    `swifts.SwiftView`  the client says it was on screen. Authoritative.

An impression with no matching view, older than the reserve window, is
re-admitted. A Swift with a view is never re-served — `watched_never_again`.

## Why not "never repeat anything ever"

Because the failure is asymmetric. Showing something twice is a small
annoyance; permanently consuming a candidate the viewer never saw is invisible
and cumulative, and it is the one that makes a feed die.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from swifts.models import SwiftView

from .models import SwiftImpression
from .params import load_params


def excluded_ids(viewer, *, params=None, now=None):
    """Swift ids that must not be served to this viewer right now.

    Two disjoint sets, unioned:

    * everything they have genuinely watched, and
    * everything served inside the reserve window, watched or not.

    Two queries, both index-only. This runs on every feed request, so it is
    written to stay that way.
    """
    params = params or load_params()
    now = now or timezone.now()
    cutoff = now - timedelta(hours=params.exposure.reserve_hours)

    watched = set()
    if params.exposure.watched_never_again:
        watched = set(
            SwiftView.objects
            .filter(viewer=viewer)
            .values_list('swift_id', flat=True)
        )

    recently_served = set(
        SwiftImpression.objects
        .filter(viewer=viewer, last_served_at__gte=cutoff)
        .values_list('swift_id', flat=True)
    )

    return watched | recently_served


def apply(viewer, candidates, *, params=None, now=None):
    """Drop candidates that are not currently eligible.

    Returns `(kept, removed_count)`, matching `safety.apply` so the pipeline
    reads the same at every filtering stage.
    """
    if not candidates:
        return [], 0
    blocked = excluded_ids(viewer, params=params, now=now)
    kept = [c for c in candidates if c.swift_id not in blocked]
    return kept, len(candidates) - len(kept)


@transaction.atomic
def mark_served(viewer, entries):
    """Record that these Swifts went out in a response.

    `entries` is a sequence of `(swift_id, source)`.

    One atomic upsert per batch, including the existing-row increment. Django's
    bulk_create update_fields only assigns proposed values; it cannot express
    that increment. PostgreSQL and SQLite both support this ON CONFLICT form.
    Duplicate ids within a page count as one serve, with the last source kept.
    """
    if not entries:
        return 0

    unique_entries = list(dict(entries).items())
    now = SwiftImpression._meta.get_field('last_served_at').get_db_prep_value(
        timezone.now(), connection)
    table = connection.ops.quote_name(SwiftImpression._meta.db_table)
    max_params = connection.features.max_query_params
    batch_size = max_params // 6 if max_params else len(unique_entries)
    with connection.cursor() as cursor:
        for start in range(0, len(unique_entries), batch_size):
            batch = unique_entries[start:start + batch_size]
            placeholders = ', '.join(['(%s, %s, %s, %s, %s, %s)'] * len(batch))
            values = [value for swift_id, source in batch
                      for value in (viewer.pk, swift_id, now, now, 1, source or '')]
            cursor.execute(
                f'INSERT INTO {table} '
                '(viewer_id, swift_id, first_served_at, last_served_at, serve_count, served_by) '
                f'VALUES {placeholders} '
                'ON CONFLICT (viewer_id, swift_id) DO UPDATE SET '
                'last_served_at = EXCLUDED.last_served_at, '
                'served_by = EXCLUDED.served_by, '
                f'serve_count = {table}.serve_count + 1',
                values,
            )
    return len(unique_entries)


def reclaim(viewer, *, params=None, now=None):
    """Delete impressions that were never watched and have aged out.

    Not required for correctness — `excluded_ids` already ignores them by
    date — but it keeps the table proportional to what a viewer actually
    watched rather than to everything ever put in front of them. Safe to run
    from a cron, safe never to run at all.
    """
    params = params or load_params()
    now = now or timezone.now()
    cutoff = now - timedelta(hours=params.exposure.reserve_hours)

    watched = SwiftView.objects.filter(
        viewer=viewer, swift_id=F('swift_id'),
    ).values_list('swift_id', flat=True)

    deleted, _ = (
        SwiftImpression.objects
        .filter(viewer=viewer, last_served_at__lt=cutoff)
        .exclude(swift_id__in=list(watched))
        .delete()
    )
    return deleted


def watched_ids(viewer, swift_ids):
    """Which of these the viewer has already watched.

    Used by feature hydration for `previous_exposure`, separately from the
    filter above — a Swift can be ineligible for several reasons and the ranker
    wants to know which.
    """
    if not swift_ids:
        return set()
    return set(
        SwiftView.objects
        .filter(viewer=viewer, swift_id__in=list(swift_ids))
        .values_list('swift_id', flat=True)
    )
