from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .models import Encounter

ENCOUNTER_COOLDOWN_MINUTES = 60
ENCOUNTER_MAX_AGE_DAYS = 3

#: How often the sweep below is allowed to actually run when it is called from
#: a request. See `delete_old_encounters_throttled`.
CLEANUP_EVERY_S = 10 * 60
_CLEANUP_KEY = 'encounters:last-cleanup'


def delete_old_encounters():
    cutoff = timezone.now() - timedelta(days=ENCOUNTER_MAX_AGE_DAYS)
    Encounter.objects.filter(timestamp__lt=cutoff).delete()


def delete_old_encounters_throttled():
    """`delete_old_encounters`, but at most once every `CLEANUP_EVERY_S`.

    The sweep was running on every `/api/proximity/nearby/` request — a DELETE
    over the whole table, taking write locks, on an endpoint the map polls every
    ten seconds per user. Nothing needed it to be that prompt: rows expire after
    three days, so running it every ten minutes is the same outcome.

    `cache.add` rather than get-then-set, so concurrent workers cannot each
    decide it is their turn. A cache outage degrades to running the sweep, which
    is the old behaviour and safe. The scheduled management command remains the
    real cleanup; this is only a backstop for deployments without a cron.
    """
    if cache.add(_CLEANUP_KEY, '1', CLEANUP_EVERY_S):
        delete_old_encounters()


def record_encounters(user, matches):
    """Log a whole sweep's encounters in a fixed number of queries.

    `matches` is `[(other_user, distance_km), ...]`. The per-pair version cost
    an `exists()` and an `INSERT` each on the hottest write path in the app; the
    cooldown rule and the canonical `user_one < user_two` ordering are unchanged.
    """
    matches = [(other, distance) for other, distance in matches if other.pk != user.pk]
    if not matches:
        return

    other_ids = [other.pk for other, _ in matches]
    cutoff = timezone.now() - timedelta(minutes=ENCOUNTER_COOLDOWN_MINUTES)
    # Every pair has `user` on one side, so one query finds every standing row.
    recent = set(
        Encounter.objects.filter(timestamp__gte=cutoff).filter(
            Q(user_one=user, user_two_id__in=other_ids)
            | Q(user_two=user, user_one_id__in=other_ids)
        ).values_list('user_one_id', 'user_two_id')
    )

    rows = []
    for other, distance in matches:
        one, two = (user, other) if user.pk < other.pk else (other, user)
        if (one.pk, two.pk) in recent:
            continue
        rows.append(Encounter(user_one=one, user_two=two, distance_km=distance))
    Encounter.objects.bulk_create(rows)


def record_encounter(user_a, user_b, distance_km: float):
    """One pair's worth of `record_encounters`."""
    record_encounters(user_a, [(user_b, distance_km)])
