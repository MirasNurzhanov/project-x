"""The decision rules, on their own.

No database, no network, no Django settings — every one of these is a pure
function over dataclasses. That is deliberate: these are the rules most likely
to be argued about and adjusted, and arguing about a threshold should cost a
test, not a fixture and a network mock.
"""
from django.test import SimpleTestCase

from . import policy
from .cloudflare_ai import ModerationResult
from .reasons import Reason
from .text import BLOCK, FLAG, Verdict


def safe(at_ms=0, confidence=0.95):
    return ModerationResult(safe=True, confidence=confidence, at_ms=at_ms)


def unsafe(categories, confidence, at_ms=0):
    return ModerationResult(
        safe=False, confidence=confidence, categories=tuple(categories), at_ms=at_ms)


# ─── Sampling ─────────────────────────────────────────────────────────────────
class FrameBudgetTests(SimpleTestCase):
    def test_a_short_clip_gets_fewer_frames_than_a_long_one(self):
        self.assertLess(
            policy.frame_count_for(4_000), policy.frame_count_for(55_000))

    def test_the_budget_is_bounded_whatever_the_duration(self):
        """The whole cost argument. A per-second strategy makes moderating a
        Swift cost sixty times more for a long one than a short one."""
        self.assertLessEqual(policy.frame_count_for(60_000, maximum=12), 12)
        self.assertLessEqual(policy.frame_count_for(10 ** 9, maximum=12), 12)

    def test_a_configured_count_wins_but_is_still_capped(self):
        self.assertEqual(policy.frame_count_for(5_000, configured=6), 6)
        self.assertEqual(policy.frame_count_for(5_000, configured=99, maximum=12), 12)

    def test_zero_duration_still_asks_for_at_least_one_frame(self):
        self.assertGreaterEqual(policy.frame_count_for(0), 1)


class SamplingTests(SimpleTestCase):
    def test_timestamps_span_the_video(self):
        stamps = policy.sample_timestamps(30_000, 8)
        self.assertEqual(len(stamps), 8)
        self.assertLess(stamps[0], 3_000)
        self.assertGreater(stamps[-1], 27_000)

    def test_the_first_and_last_portions_are_sampled(self):
        """Requirement 3 by name: a video whose only objectionable moment is in
        the first or last second must not be sampled exclusively through the
        middle."""
        duration = 20_000
        stamps = policy.sample_timestamps(duration, 6)
        self.assertLessEqual(stamps[0], duration * 0.1)
        self.assertGreaterEqual(stamps[-1], duration * 0.9)

    def test_timestamps_are_ordered_and_distinct(self):
        stamps = policy.sample_timestamps(45_000, 10)
        self.assertEqual(list(stamps), sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))

    def test_a_very_short_clip_collapses_rather_than_repeating_a_frame(self):
        """Rendered to one decimal place, six timestamps across 1.2s collide.
        Paying twice to classify the same frame is waste, not thoroughness."""
        stamps = policy.sample_timestamps(1_200, 8)
        self.assertEqual(len(set(stamps)), len(stamps))
        self.assertLessEqual(len(stamps), 8)

    def test_no_duration_does_not_crash(self):
        self.assertTrue(policy.sample_timestamps(0, 5))


# ─── Combining frame results ──────────────────────────────────────────────────
class CombineTests(SimpleTestCase):
    def test_safe_frames_cannot_outvote_an_invalid_verdict(self):
        decision = policy.combine_frames(
            [safe(0), safe(1000), safe(2000), safe(3000)],
            requested=5, invalid_responses=1)
        self.assertEqual(decision.outcome, policy.FAILED)
        self.assertEqual(decision.basis, policy.INVALID_RESPONSE)
        self.assertEqual(decision.frames_classified, 4)
        self.assertEqual(decision.sampled_ms, (0, 1000, 2000, 3000))

    def test_a_valid_unsafe_frame_still_quarantines_amid_invalid_answers(self):
        decision = policy.combine_frames(
            [unsafe(['nudity'], 0.9, at_ms=1000)], requested=5, invalid_responses=4)
        self.assertEqual(decision.outcome, policy.QUARANTINED)

    def test_all_frames_safe_is_an_approval(self):
        decision = policy.combine_frames(
            [safe(0), safe(1000), safe(2000), safe(3000)], requested=4)
        self.assertEqual(decision.outcome, policy.APPROVED)
        self.assertTrue(decision.publishable)
        self.assertEqual(decision.frames_classified, 4)

    def test_one_confident_unsafe_frame_quarantines(self):
        decision = policy.combine_frames(
            [safe(0), unsafe(['nudity'], 0.93, at_ms=2000), safe(4000)],
            requested=3)
        self.assertEqual(decision.outcome, policy.QUARANTINED)
        self.assertEqual(decision.basis, policy.UNSAFE_CONFIDENT)
        self.assertEqual(decision.reason, Reason.NUDITY)
        self.assertFalse(decision.publishable)

    def test_one_weak_unsafe_frame_quarantines_as_uncertain(self):
        """It hides the Swift either way — the difference is the queue note. A
        moderator reading every item as equally damning is a moderator who
        cannot triage."""
        decision = policy.combine_frames(
            [safe(0), unsafe(['graphic_violence'], 0.35, at_ms=1000)], requested=2)
        self.assertEqual(decision.outcome, policy.QUARANTINED)
        self.assertEqual(decision.basis, policy.UNSAFE_UNCERTAIN)

    def test_model_evidence_and_timestamp_come_from_the_strongest_unsafe_frame(self):
        decision = policy.combine_frames([
            ModerationResult(
                safe=False, confidence=0.35, categories=('weapons_threat',),
                at_ms=1000, evidence='A distant person may hold a weapon.'),
            ModerationResult(
                safe=False, confidence=0.92, categories=('graphic_violence',),
                at_ms=2000, evidence='A visible wound is bleeding.'),
            ModerationResult(
                safe=True, confidence=0.99, at_ms=3000,
                description='A closed door.'),
        ], requested=3)
        self.assertEqual(decision.evidence, 'A visible wound is bleeding.')
        self.assertEqual(decision.evidence_at_ms, 2000)
        self.assertEqual(decision.confidence, 0.92)
        self.assertEqual(decision.outcome, policy.QUARANTINED)

    def test_older_frame_results_without_evidence_still_quarantine(self):
        decision = policy.combine_frames(
            [unsafe(['nudity'], 0.9, at_ms=2000)], requested=1)
        self.assertEqual(decision.evidence, '')
        self.assertIsNone(decision.evidence_at_ms)
        self.assertEqual(decision.outcome, policy.QUARANTINED)

    def test_two_weak_frames_agreeing_is_corroboration(self):
        decision = policy.combine_frames(
            [unsafe(['nudity'], 0.4, at_ms=0), unsafe(['nudity'], 0.45, at_ms=9000)],
            requested=4)
        self.assertEqual(decision.basis, policy.UNSAFE_CORROBORATED)

    def test_no_frame_result_ever_removes_or_bans(self):
        """There is deliberately no fourth outcome. A classifier that could ban
        people would be one whose false positive rate is somebody's account."""
        for confidence in (0.5, 0.9, 1.0):
            decision = policy.combine_frames(
                [unsafe(['minor_safety'], confidence)], requested=1)
            self.assertIn(decision.outcome, (policy.QUARANTINED, policy.FAILED))

    def test_the_worst_category_names_the_report(self):
        decision = policy.combine_frames([
            unsafe(['other_objectionable'], 0.9, at_ms=0),
            unsafe(['minor_safety'], 0.8, at_ms=5000),
        ], requested=2)
        self.assertEqual(decision.reason, Reason.MINOR_SAFETY)

    def test_too_few_frames_is_a_failure_not_an_approval(self):
        """The fail-open bug this exists to prevent: eight frames requested,
        one classified, everything that came back was clean. That is not a
        review of the video."""
        decision = policy.combine_frames([safe(0)], requested=8)
        self.assertEqual(decision.outcome, policy.FAILED)
        self.assertFalse(decision.publishable)

    def test_no_frames_at_all_is_a_failure(self):
        decision = policy.combine_frames([], requested=6)
        self.assertEqual(decision.outcome, policy.FAILED)

    def test_a_partial_but_sufficient_run_still_approves(self):
        decision = policy.combine_frames(
            [safe(0), safe(2000), safe(4000), safe(6000)], requested=6)
        self.assertEqual(decision.outcome, policy.APPROVED)

    def test_an_unsafe_frame_counts_even_in_a_barely_sampled_run(self):
        """The sufficiency rule protects against false approvals only. One
        frame is plenty to justify asking a human."""
        decision = policy.combine_frames(
            [unsafe(['nudity'], 0.9)], requested=8)
        self.assertEqual(decision.outcome, policy.QUARANTINED)

    def test_an_uncategorised_objection_is_not_treated_as_safe(self):
        decision = policy.combine_frames(
            [safe(0), ModerationResult(safe=False, confidence=0.8, at_ms=1000)],
            requested=2)
        self.assertEqual(decision.outcome, policy.QUARANTINED)
        self.assertEqual(decision.reason, Reason.OTHER)

    def test_sampled_timestamps_are_carried_into_the_decision(self):
        decision = policy.combine_frames(
            [safe(0), safe(5000), safe(9000)], requested=3)
        self.assertEqual(decision.sampled_ms, (0, 5000, 9000))


# ─── Text folded in ───────────────────────────────────────────────────────────
class TextVerdictTests(SimpleTestCase):
    def test_a_safe_video_with_a_flagged_caption_is_quarantined(self):
        """Requirement 10: the two judgements are separate and either can hold
        a Swift back."""
        approved = policy.combine_frames([safe(0), safe(1000)], requested=2)
        decision = policy.apply_text_verdict(
            approved, Verdict(FLAG, Reason.HATE, ('hate.x',)), basis=policy.CAPTION)
        self.assertEqual(decision.outcome, policy.QUARANTINED)
        self.assertEqual(decision.reason, Reason.HATE)
        self.assertEqual(decision.basis, policy.CAPTION)

    def test_an_allowing_verdict_changes_nothing(self):
        approved = policy.combine_frames([safe(0), safe(1000)], requested=2)
        self.assertIs(policy.apply_text_verdict(
            approved, Verdict(), basis=policy.CAPTION), approved)

    def test_a_video_quarantine_is_never_downgraded_by_a_clean_caption(self):
        held = policy.combine_frames([unsafe(['nudity'], 0.9)], requested=1)
        decision = policy.apply_text_verdict(held, Verdict(), basis=policy.CAPTION)
        self.assertEqual(decision.outcome, policy.QUARANTINED)

    def test_a_failed_run_is_not_closed_by_a_text_verdict(self):
        """Closing the job on the strength of the caption would leave the
        frames permanently unclassified."""
        failed = policy.combine_frames([], requested=6)
        decision = policy.apply_text_verdict(
            failed, Verdict(BLOCK, Reason.VIOLENCE, ('threat.x',)),
            basis=policy.TRANSCRIPT)
        self.assertEqual(decision.outcome, policy.FAILED)

    def test_a_transcript_verdict_records_its_own_basis(self):
        approved = policy.combine_frames([safe(0), safe(1000)], requested=2)
        decision = policy.apply_text_verdict(
            approved, Verdict(BLOCK, Reason.SELF_HARM, ('x',)),
            basis=policy.TRANSCRIPT)
        self.assertEqual(decision.basis, policy.TRANSCRIPT)
        self.assertEqual(decision.outcome, policy.QUARANTINED)
