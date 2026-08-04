"""The encounter log, batched.

`record_encounters` replaced a per-pair function costing an existence check and
an insert each, on the endpoint every phone reports to every thirty seconds. The
rules it must keep: canonical `user_one < user_two` ordering, one row per pair
per cooldown window, and a cleanup sweep that no longer runs on every poll.
"""
from datetime import date, timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from encounters.models import Encounter
from encounters.utils import (
    ENCOUNTER_COOLDOWN_MINUTES, ENCOUNTER_MAX_AGE_DAYS, delete_old_encounters,
    delete_old_encounters_throttled, record_encounter, record_encounters,
)
from users.models import User


def make_user(name):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='pw',
        date_of_birth=date(1995, 1, 1),
    )


class RecordEncountersTests(TestCase):
    def setUp(self):
        self.me = make_user('me')
        self.other = make_user('other')

    def test_records_the_pair_in_canonical_order(self):
        record_encounters(self.other, [(self.me, 0.2)])
        encounter = Encounter.objects.get()
        # Lower pk first, whichever way round the call was made.
        self.assertEqual(encounter.user_one_id, min(self.me.pk, self.other.pk))
        self.assertEqual(encounter.user_two_id, max(self.me.pk, self.other.pk))

    def test_cooldown_suppresses_a_second_row(self):
        record_encounters(self.me, [(self.other, 0.2)])
        record_encounters(self.me, [(self.other, 0.3)])
        self.assertEqual(Encounter.objects.count(), 1)

    def test_cooldown_is_direction_independent(self):
        """The reverse call must see the row the forward one wrote."""
        record_encounters(self.me, [(self.other, 0.2)])
        record_encounters(self.other, [(self.me, 0.2)])
        self.assertEqual(Encounter.objects.count(), 1)

    def test_records_again_once_the_cooldown_has_passed(self):
        record_encounters(self.me, [(self.other, 0.2)])
        Encounter.objects.update(
            timestamp=timezone.now() - timedelta(minutes=ENCOUNTER_COOLDOWN_MINUTES + 1),
        )
        record_encounters(self.me, [(self.other, 0.3)])
        self.assertEqual(Encounter.objects.count(), 2)

    def test_self_is_never_recorded(self):
        record_encounters(self.me, [(self.me, 0.0)])
        self.assertEqual(Encounter.objects.count(), 0)

    def test_single_pair_wrapper_matches_the_batch(self):
        record_encounter(self.me, self.other, 0.2)
        self.assertEqual(Encounter.objects.count(), 1)

    def test_query_count_does_not_grow_with_the_sweep(self):
        crowd = [(make_user(f'p{i}'), 0.1) for i in range(12)]
        with self.assertNumQueries(2):
            record_encounters(self.me, crowd)
        self.assertEqual(Encounter.objects.count(), 12)

    def test_empty_sweep_touches_the_database_not_at_all(self):
        with self.assertNumQueries(0):
            record_encounters(self.me, [])


class CleanupThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = make_user('me')
        self.other = make_user('other')

    def _stale_encounter(self):
        Encounter.objects.create(
            user_one=self.me, user_two=self.other, distance_km=0.1,
        )
        Encounter.objects.update(
            timestamp=timezone.now() - timedelta(days=ENCOUNTER_MAX_AGE_DAYS + 1),
        )

    def test_sweep_deletes_expired_rows(self):
        self._stale_encounter()
        delete_old_encounters()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_sweep_keeps_fresh_rows(self):
        Encounter.objects.create(
            user_one=self.me, user_two=self.other, distance_km=0.1,
        )
        delete_old_encounters()
        self.assertEqual(Encounter.objects.count(), 1)

    def test_throttled_sweep_runs_the_first_time(self):
        self._stale_encounter()
        delete_old_encounters_throttled()
        self.assertEqual(Encounter.objects.count(), 0)

    def test_throttled_sweep_does_not_run_again_immediately(self):
        delete_old_encounters_throttled()
        self._stale_encounter()
        # The second call inside the window is a no-op, so the stale row stays.
        delete_old_encounters_throttled()
        self.assertEqual(Encounter.objects.count(), 1)
        # The unthrottled sweep — the scheduled command — still clears it.
        delete_old_encounters()
        self.assertEqual(Encounter.objects.count(), 0)
