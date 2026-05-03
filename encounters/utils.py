from django.utils import timezone
from datetime import timedelta
from .models import Encounter

ENCOUNTER_COOLDOWN_MINUTES = 60


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
