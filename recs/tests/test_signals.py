"""Signal ingestion, and the affinity summary it maintains.

The property under test throughout is that `CreatorAffinity` never disagrees
with the `SwiftSignal` rows it summarises — because the moment it does, every
ranking decision downstream is made on a number nobody can reconstruct.
"""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from ..ingest import decayed_score, normalise, record
from ..models import CreatorAffinity, SwiftSignal
from ..testing import SwiftTestCase, make_swift, make_user


class NormalisationTests(SwiftTestCase):
    """The server decides what a watch meant, not the client."""

    def test_a_barely_watched_video_is_a_skip(self):
        self.assertEqual(
            normalise(SwiftSignal.Kind.WATCH, 3), SwiftSignal.Kind.SKIP,
        )

    def test_a_partly_watched_video_is_a_low_watch(self):
        self.assertEqual(
            normalise(SwiftSignal.Kind.WATCH, 25), SwiftSignal.Kind.LOW_WATCH,
        )

    def test_a_properly_watched_video_stays_a_watch(self):
        self.assertEqual(
            normalise(SwiftSignal.Kind.WATCH, 80), SwiftSignal.Kind.WATCH,
        )

    def test_taps_are_never_reinterpreted(self):
        for kind in (SwiftSignal.Kind.LIKE, SwiftSignal.Kind.REPOST,
                     SwiftSignal.Kind.COMMENT):
            self.assertEqual(normalise(kind, 0), kind)

    def test_the_stored_kind_is_the_normalised_one(self):
        viewer, author = make_user('v'), make_user('a')
        swift = make_swift(author)

        signal = record(viewer, SwiftSignal.Kind.WATCH, swift=swift, completion_pct=2)

        self.assertEqual(signal.kind, SwiftSignal.Kind.SKIP)


class AffinityTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')
        self.swift = make_swift(self.author)

    def affinity(self):
        return CreatorAffinity.objects.get(viewer=self.viewer, creator=self.author)

    def test_a_like_creates_a_positive_affinity(self):
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)

        affinity = self.affinity()
        self.assertGreater(affinity.score, 0)
        self.assertEqual(affinity.positive_count, 1)

    def test_a_skip_creates_a_negative_one(self):
        record(self.viewer, SwiftSignal.Kind.WATCH, swift=self.swift, completion_pct=1)

        affinity = self.affinity()
        self.assertLess(affinity.score, 0)
        self.assertEqual(affinity.negative_count, 1)
        self.assertEqual(affinity.skip_streak, 1)

    def test_skips_accumulate_into_a_streak(self):
        for _ in range(4):
            record(self.viewer, SwiftSignal.Kind.WATCH, swift=self.swift, completion_pct=1)

        self.assertEqual(self.affinity().skip_streak, 4)

    def test_a_positive_ends_the_streak(self):
        for _ in range(3):
            record(self.viewer, SwiftSignal.Kind.WATCH, swift=self.swift, completion_pct=1)
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)

        self.assertEqual(self.affinity().skip_streak, 0)

    def test_a_watch_is_worth_what_was_watched(self):
        partial = make_user('partial')
        full = make_user('full')
        record(self.viewer, SwiftSignal.Kind.WATCH,
               swift=make_swift(partial), completion_pct=50)
        record(self.viewer, SwiftSignal.Kind.WATCH,
               swift=make_swift(full), completion_pct=100)

        self.assertLess(
            CreatorAffinity.objects.get(viewer=self.viewer, creator=partial).score,
            CreatorAffinity.objects.get(viewer=self.viewer, creator=full).score,
        )

    def test_not_interested_suppresses_permanently(self):
        record(self.viewer, SwiftSignal.Kind.NOT_INTERESTED, creator=self.author)
        self.assertTrue(self.affinity().suppressed)

        # Later positives do not undo it — only the viewer can.
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)
        self.assertTrue(self.affinity().suppressed)

    def test_a_creator_level_signal_needs_no_swift(self):
        signal = record(self.viewer, SwiftSignal.Kind.BLOCK_CREATOR, creator=self.author)

        self.assertIsNone(signal.swift_id)
        self.assertTrue(self.affinity().suppressed)

    def test_signals_about_yourself_move_nothing(self):
        mine = make_swift(self.viewer)
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=mine)

        self.assertFalse(
            CreatorAffinity.objects.filter(viewer=self.viewer, creator=self.viewer).exists()
        )

    def test_the_log_is_append_only(self):
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)

        self.assertEqual(
            SwiftSignal.objects.filter(viewer=self.viewer, kind='like').count(), 2,
        )

    def test_the_creator_is_denormalised_onto_the_signal(self):
        signal = record(self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift)
        self.assertEqual(signal.creator_id, self.author.pk)

    def test_serving_context_is_kept_for_training(self):
        """Position and source, without which a model learns the feed's own
        ordering rather than the viewer's taste."""
        signal = record(
            self.viewer, SwiftSignal.Kind.LIKE, swift=self.swift,
            served_by='trending', position=4,
        )
        self.assertEqual(signal.served_by, 'trending')
        self.assertEqual(signal.position, 4)


class DecayTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def test_an_old_affinity_reads_lower_than_it_was_written(self):
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=make_swift(self.author))
        affinity = CreatorAffinity.objects.get(viewer=self.viewer, creator=self.author)
        raw = affinity.score

        affinity.last_signal_at = timezone.now() - timedelta(days=28)
        aged = decayed_score(affinity)

        self.assertLess(aged, raw)
        # Two half-lives at the default of fourteen days.
        self.assertAlmostEqual(aged, raw * 0.25, places=3)

    def test_a_fresh_affinity_is_not_decayed(self):
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=make_swift(self.author))
        affinity = CreatorAffinity.objects.get(viewer=self.viewer, creator=self.author)

        self.assertAlmostEqual(decayed_score(affinity), affinity.score, places=6)

    @override_settings(SWIFT_RECS={'limits': {'affinity_half_life_days': 1.0}})
    def test_the_half_life_is_configurable(self):
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=make_swift(self.author))
        affinity = CreatorAffinity.objects.get(viewer=self.viewer, creator=self.author)
        raw = affinity.score
        affinity.last_signal_at = timezone.now() - timedelta(days=1)

        self.assertAlmostEqual(decayed_score(affinity), raw * 0.5, places=3)

    def test_decay_is_applied_on_write_not_compounded(self):
        """Writing twice, a long gap apart, must not decay the first twice.

        The write ages the stored value forward *and* moves `last_signal_at`,
        so the read-time decay starts from the new timestamp. Getting this
        wrong is silent and halves everybody's history.
        """
        record(self.viewer, SwiftSignal.Kind.LIKE, swift=make_swift(self.author))
        CreatorAffinity.objects.filter(viewer=self.viewer, creator=self.author).update(
            last_signal_at=timezone.now() - timedelta(days=14),
        )

        record(self.viewer, SwiftSignal.Kind.LIKE, swift=make_swift(self.author))
        affinity = CreatorAffinity.objects.get(viewer=self.viewer, creator=self.author)

        # Half of the first like, plus a whole second one, and no further decay
        # because the timestamp moved.
        expected = SwiftSignal.polarity_for(SwiftSignal.Kind.LIKE) * 1.5
        self.assertAlmostEqual(affinity.score, expected, places=3)
        self.assertAlmostEqual(decayed_score(affinity), expected, places=3)
