"""Point ledger: the single writer for Aura points.

Every movement is an immutable PointEvent row; UserAuraPoints.total_points is
a cache recomputed from those rows, so the displayed total is always
explainable and can be rebuilt if it ever drifts.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone

from .models import PointEvent, UserAuraPoints

LEADERBOARD_SIZE = 50


def week_start_for(date):
    """Monday of the ISO week containing `date`."""
    return date - timedelta(days=date.weekday())


def current_week_start(now=None):
    return week_start_for(timezone.localdate(now or timezone.now()))


def _recompute_total(user):
    total = PointEvent.objects.filter(user=user).aggregate(t=Sum('points'))['t'] or 0
    total = max(0, total)  # the cache is a PositiveIntegerField; ledger is truth
    UserAuraPoints.objects.update_or_create(user=user, defaults={'total_points': total})
    return total


@transaction.atomic
def record_points(user, points, reason, quest=None, proof=None, period_start=None):
    """Append a ledger event and refresh the user's cached total."""
    PointEvent.objects.create(
        user=user, quest=quest, proof=proof,
        points=points, reason=reason, period_start=period_start,
    )
    return _recompute_total(user)


def award_for_proof(proof):
    """Points for an approved proof, credited to the week the quest window
    belonged to. Returns the user's new all-time total."""
    return record_points(
        proof.user,
        proof.quest.reward_points,
        PointEvent.Reason.QUEST_APPROVED,
        quest=proof.quest,
        proof=proof,
        period_start=week_start_for(proof.window_start),
    )


def revoke_for_proof(proof):
    """Reverse a previously approved proof — same magnitude, same period, so
    the weekly leaderboard nets to zero for that completion."""
    awarded = PointEvent.objects.filter(
        proof=proof, reason=PointEvent.Reason.QUEST_APPROVED,
    ).aggregate(t=Sum('points'))['t'] or 0
    already_revoked = PointEvent.objects.filter(
        proof=proof, reason=PointEvent.Reason.QUEST_REVOKED,
    ).aggregate(t=Sum('points'))['t'] or 0
    outstanding = awarded + already_revoked  # revocations are negative
    if outstanding <= 0:
        return None  # nothing left to take back
    return record_points(
        proof.user,
        -outstanding,
        PointEvent.Reason.QUEST_REVOKED,
        quest=proof.quest,
        proof=proof,
        period_start=week_start_for(proof.window_start),
    )


def weekly_standings(week_start, limit=LEADERBOARD_SIZE, exclude_user_ids=()):
    """Top scorers for one week, deterministically ordered.

    Ties break by who reached their total first, then by user id — so the
    same data always produces the same ranking. Only rows for this week are
    touched (indexed on period_start), never a full user scan.

    `exclude_user_ids` drops people before the limit is applied, so hiding
    someone does not leave a short page or a gap in the ranks.
    """
    qs = PointEvent.objects.filter(period_start=week_start)
    if exclude_user_ids:
        qs = qs.exclude(user_id__in=exclude_user_ids)
    return list(
        qs.values('user_id')
        .annotate(total=Sum('points'), first_at=Min('created_at'))
        .filter(total__gt=0)
        .order_by('-total', 'first_at', 'user_id')[:limit]
    )


def weekly_total_for(user, week_start):
    return PointEvent.objects.filter(
        user=user, period_start=week_start,
    ).aggregate(t=Sum('points'))['t'] or 0


def weekly_rank_for(user, week_start, exclude_user_ids=()):
    """1-based rank, or None when the user has no points this week.

    Returns None for the rank when the user is themselves excluded — someone
    who opted out of the leaderboard still sees their score, but has no
    position on a board they are not on.
    """
    my_total = weekly_total_for(user, week_start)
    if my_total <= 0:
        return None, my_total
    if user.pk in set(exclude_user_ids):
        return None, my_total
    qs = PointEvent.objects.filter(period_start=week_start)
    if exclude_user_ids:
        qs = qs.exclude(user_id__in=exclude_user_ids)
    ahead = (
        qs.values('user_id')
        .annotate(total=Sum('points'))
        .filter(total__gt=my_total)
        .count()
    )
    return ahead + 1, my_total
