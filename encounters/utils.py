from datetime import timedelta

from django.utils import timezone

from .models import Encounter
from quests.utils import check_and_complete_quest

ENCOUNTER_COOLDOWN_MINUTES = 60
ENCOUNTER_MAX_AGE_DAYS = 3


def delete_old_encounters():
    cutoff = timezone.now() - timedelta(days=ENCOUNTER_MAX_AGE_DAYS)
    Encounter.objects.filter(timestamp__lt=cutoff).delete()


def record_encounter(user_a, user_b, distance_km: float):
    """
    Persist an encounter between two users with canonical ordering.
    Skipped if the same pair already has an encounter within the cooldown window.
    """
    user_one, user_two = (user_a, user_b) if user_a.pk < user_b.pk else (user_b, user_a)

    cutoff = timezone.now() - timedelta(minutes=ENCOUNTER_COOLDOWN_MINUTES)
    already_recorded = Encounter.objects.filter(
        user_one=user_one,
        user_two=user_two,
        timestamp__gte=cutoff,
    ).exists()

    if not already_recorded:
        Encounter.objects.create(
            user_one=user_one,
            user_two=user_two,
            distance_km=distance_km,
        )
        check_and_complete_quest(user_a, 'have_encounter')
        check_and_complete_quest(user_b, 'have_encounter')
