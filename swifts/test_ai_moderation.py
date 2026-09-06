"""AI video moderation, end to end, with Cloudflare replaced by a stub.

Nothing in this file touches the network. `patched_cloudflare` swaps the three
functions in `moderation.cloudflare_ai` that would leave the machine, and the
credentials in `AI_ON` are a recognisable fake — `NoLeakTests` at the bottom is
what checks that a real one could not reach a response body or a log line.

The rule every test here is really about is one sentence long: **a Swift must
not be watchable by anybody but its author until the classifier has approved
it.** Most of these are that sentence pointed at a different surface — the
feed, the detail endpoint, the recommender, reposting, commenting, the reveal
— because a gate that holds on four of six surfaces is not a gate.
"""
import logging
from contextlib import contextmanager
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from moderation import cloudflare_ai, policy
from moderation.cloudflare_ai import CloudflareError, ModerationResult, TranscriptResult
from moderation.reasons import Moderation

from swifts import ai_moderation
from .models import SwiftModerationJob, SwiftReport, SwiftVideo
from .testing import (
    SwiftTestCase, befriend, block, fake_video, make_swift, make_user,
)

FAKE_TOKEN = 'cf-test-token-NEVER-REAL-0000'

#: Moderation on, credentials present, and the worker running inline so a test
#: asserts on a finished state rather than on a sleep. Same arrangement, and
#: the same reason, as `PUSH_DISABLED` in `notifications.push`.
AI_ON = dict(
    SWIFT_AI_MODERATION_ENABLED=True,
    SWIFT_AI_MODERATION_SYNC=True,
    CLOUDFLARE_API_TOKEN=FAKE_TOKEN,
    CLOUDFLARE_ACCOUNT_ID='acct-test-0000',
    CLOUDFLARE_MEDIA_TRANSFORM_BASE='https://media.example.test',
    # In production `swift.video.url` is an absolute R2 URL, because
    # `S3Boto3Storage` is configured with a `custom_domain`. The test settings
    # swap in `FileSystemStorage`, whose `.url` is `MEDIA_URL` + name — so
    # without this the pipeline would refuse every fixture as "not at an
    # absolute URL Cloudflare can fetch", and the tests would be measuring that
    # refusal instead of the thing they are about.
    MEDIA_URL='https://cdn.test.example/media/',
)


@contextmanager
def patched_cloudflare(*, classify=None, frame=None, audio=None, transcribe=None):
    """Replace every function in the client that would reach the network.

    Patched on the `moderation.cloudflare_ai` module rather than on `requests`,
    so a test that accidentally reaches a *new* network call fails loudly
    instead of silently making one.
    """
    def default_frame(source_url, at_ms, **kwargs):
        return b'\xff\xd8fake-jpeg'

    def default_classify(image_bytes, at_ms=None, prompt=None):
        return ModerationResult(safe=True, confidence=0.97, at_ms=at_ms)

    with patch.object(cloudflare_ai, 'fetch_frame', frame or default_frame), \
            patch.object(cloudflare_ai, 'classify_image', classify or default_classify), \
            patch.object(cloudflare_ai, 'fetch_audio',
                         audio or (lambda url, duration_s=60: b'audio')), \
            patch.object(cloudflare_ai, 'transcribe',
                         transcribe or (lambda data: TranscriptResult(''))):
        yield


def unsafe_classifier(categories, confidence):
    def classify(image_bytes, at_ms=None, prompt=None):
        return ModerationResult(
            safe=False, confidence=confidence,
            categories=tuple(categories), at_ms=at_ms)
    return classify


def alternating_classifier(unsafe_at, categories, confidence):
    """Unsafe only at the listed timestamps. For "one bad frame" cases."""
    def classify(image_bytes, at_ms=None, prompt=None):
        if at_ms in unsafe_at:
            return ModerationResult(
                safe=False, confidence=confidence,
                categories=tuple(categories), at_ms=at_ms)
        return ModerationResult(safe=True, confidence=0.96, at_ms=at_ms)
    return classify


class AIModerationTestCase(SwiftTestCase):
    """A creator with a client, and a stranger to check visibility against."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.stranger = make_user('stranger')
        self.client = APIClient()
        self.client.force_authenticate(self.creator)
        self.other = APIClient()
        self.other.force_authenticate(self.stranger)

    def post_swift(self, **extra):
        payload = {
            'video': fake_video(),
            'duration_ms': 15_000,
            'width': 1080,
            'height': 1920,
            'caption': 'a swift',
            **extra,
        }
        return self.client.post(reverse('swift-create'), payload, format='multipart')

    def feed_ids(self, client=None):
        response = (client or self.other).get(reverse('swift-feed'), {'chrono': 1})
        return [row['id'] for row in response.json()['results']]


# ─── Requirement 7: nothing is public before a verdict ────────────────────────
@override_settings(**AI_ON)
class PendingIsInvisibleTests(AIModerationTestCase):
    def test_a_new_swift_starts_pending_and_in_no_feed(self):
        """The window this feature exists to close. There is no moment at which
        the row exists in a publishable state — `moderation=PENDING` is written
        in the same INSERT as the row."""
        with patched_cloudflare():
            # The classifier is prevented from running so the PENDING state can
            # be observed; the enqueue path is covered separately below.
            with patch.object(ai_moderation, 'start', lambda job_id: None):
                response = self.post_swift()

        self.assertEqual(response.status_code, 201)
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertFalse(swift.is_live)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_a_pending_swift_is_a_404_to_everybody_else(self):
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        detail = reverse('swift-detail', args=[swift.pk])
        self.assertEqual(self.other.get(detail).status_code, 404)

    def test_a_pending_swift_is_a_404_to_a_friend_too(self):
        """Audience is a separate question from lifecycle, and the lifecycle
        gate is the one that has to hold here."""
        befriend(self.creator, self.stranger)
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        self.assertEqual(
            self.other.get(reverse('swift-detail', args=[swift.pk])).status_code, 404)

    def test_every_engagement_surface_refuses_a_pending_swift(self):
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        for name, method in (
            ('swift-like', 'post'),
            ('swift-repost', 'post'),
            ('swift-comments', 'get'),
            ('swift-view', 'post'),
            ('swift-report', 'post'),
            ('swift-reveal-location', 'post'),
        ):
            with self.subTest(endpoint=name):
                response = getattr(self.other, method)(reverse(name, args=[swift.pk]))
                self.assertEqual(response.status_code, 404, name)

    def test_the_recommender_never_offers_a_pending_swift(self):
        from recs import safety

        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        self.assertEqual(safety.permitted_ids(self.stranger, [swift.pk]), set())

    def test_the_author_can_still_see_their_own_pending_swift(self):
        """The composer navigates straight to the permalink after publishing.
        A 404 there would read as "your upload vanished"."""
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        response = self.client.get(reverse('swift-detail', args=[swift.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['review_state'], 'reviewing')

    def test_the_author_sees_it_in_their_own_list(self):
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        ids = [r['id'] for r in self.client.get(reverse('swift-mine')).json()['results']]
        self.assertIn(swift.pk, ids)

    def test_a_removed_swift_is_hidden_from_its_author_too(self):
        """The author exception is for content awaiting a verdict, not for
        content that has had one. A takedown the author can still browse is not
        a takedown."""
        swift = make_swift(self.creator, moderation=Moderation.REMOVED)
        self.assertEqual(
            self.client.get(reverse('swift-detail', args=[swift.pk])).status_code, 404)

    def test_other_viewers_are_never_told_a_review_state(self):
        swift = make_swift(self.creator)
        body = self.other.get(reverse('swift-detail', args=[swift.pk])).json()
        self.assertEqual(body['review_state'], 'published')


# ─── The pipeline's outcomes ──────────────────────────────────────────────────
@override_settings(**AI_ON)
class PipelineOutcomeTests(AIModerationTestCase):
    def run_pipeline(self, **cloudflare):
        with patched_cloudflare(**cloudflare):
            response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        return swift, SwiftModerationJob.objects.get(swift=swift)

    def test_a_safe_result_approves_and_publishes(self):
        swift, job = self.run_pipeline()
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertTrue(swift.is_live)
        self.assertEqual(job.outcome, policy.APPROVED)
        self.assertEqual(job.state, SwiftModerationJob.State.DONE)
        self.assertIn(swift.pk, self.feed_ids())

    def test_a_confident_unsafe_result_quarantines_and_stays_hidden(self):
        swift, job = self.run_pipeline(
            classify=unsafe_classifier(['sexual_content'], 0.95))
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertFalse(swift.is_live)
        self.assertEqual(job.basis, policy.UNSAFE_CONFIDENT)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_trimmed_uploads_sample_and_record_the_visible_source_range(self):
        expected = [10_400, 12_200, 14_000, 15_800, 17_600]
        for metadata in (
            {'duration_ms': 30_000, 'trim_end_ms': 18_000},
            {'duration_ms': 18_000},
        ):
            with self.subTest(metadata=metadata):
                fetched = []

                def record_frame(source_url, at_ms, **kwargs):
                    fetched.append(at_ms)
                    return b'\xff\xd8fake-jpeg'

                with patched_cloudflare(frame=record_frame):
                    response = self.post_swift(trim_start_ms=10_000, **metadata)

                self.assertEqual(response.status_code, 201, response.data)
                swift = SwiftVideo.objects.get(pk=response.json()['id'])
                job = SwiftModerationJob.objects.get(swift=swift)
                self.assertEqual(fetched, expected)
                self.assertEqual(job.sampled_timestamps, expected)
                self.assertEqual(job.frames_requested, 5)
                self.assertEqual(swift.moderation, Moderation.OK)

    def test_an_uncertain_result_also_quarantines(self):
        """A single weak frame hides the Swift and asks a human. It does not
        remove anything and it does not touch the account."""
        swift, job = self.run_pipeline(
            classify=alternating_classifier(
                {policy.sample_timestamps(15_000, policy.frame_count_for(15_000))[1]},
                ['graphic_violence'], 0.35))
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertEqual(job.basis, policy.UNSAFE_UNCERTAIN)
        self.creator.refresh_from_db()
        self.assertIsNone(self.creator.suspended_at)

    def test_nothing_the_ai_decides_ever_removes_content(self):
        for confidence in (0.4, 0.8, 1.0):
            with self.subTest(confidence=confidence):
                swift, _ = self.run_pipeline(
                    classify=unsafe_classifier(['minor_safety'], confidence))
                self.assertNotEqual(swift.moderation, Moderation.REMOVED)

    def test_a_timeout_leaves_the_swift_unpublished(self):
        def timeout_frame(source_url, at_ms, **kwargs):
            raise CloudflareError('Timed out fetching frame.', retryable=True)

        swift, job = self.run_pipeline(frame=timeout_frame)
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertFalse(swift.is_live)
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertIsNotNone(job.next_attempt_at)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_a_malformed_cloudflare_response_leaves_the_swift_unpublished(self):
        def gibberish(image_bytes, at_ms=None, prompt=None):
            raise CloudflareError('Vision model returned unparseable JSON.',
                                  retryable=True)

        swift, job = self.run_pipeline(classify=gibberish)
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_missing_credentials_leave_the_swift_unpublished(self):
        """A mistyped variable must not publish everything. It publishes
        nothing, which is the only acceptable direction to fail in."""
        def no_creds(image_bytes, at_ms=None, prompt=None):
            raise cloudflare_ai.MissingCredentials('CLOUDFLARE_API_TOKEN')

        swift, job = self.run_pipeline(classify=no_creds)
        self.assertNotEqual(swift.moderation, Moderation.OK)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_a_partly_failed_run_is_not_an_approval(self):
        """Seven of eight frames failing and the eighth coming back clean is
        not a review of the video."""
        calls = {'n': 0}

        def flaky(source_url, at_ms, **kwargs):
            calls['n'] += 1
            if calls['n'] > 1:
                raise CloudflareError('nope', retryable=True)
            return b'\xff\xd8'

        swift, job = self.run_pipeline(frame=flaky)
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)

    def test_the_job_records_what_the_classifier_looked_at(self):
        swift, job = self.run_pipeline()
        self.assertGreaterEqual(len(job.sampled_timestamps), 2)
        self.assertEqual(job.frames_classified, job.frames_requested)
        self.assertTrue(job.model_used)

    def test_frames_are_sampled_across_the_video_not_at_one_point(self):
        seen = []

        def record(source_url, at_ms, **kwargs):
            seen.append(at_ms)
            return b'\xff\xd8'

        self.run_pipeline(frame=record)
        self.assertGreaterEqual(len(seen), 5)
        self.assertLess(seen[0], 3_000)
        self.assertGreater(seen[-1], 12_000)

    def test_the_frame_budget_is_bounded(self):
        """Requirement 3's cost rule, measured rather than asserted about."""
        seen = []

        def record(source_url, at_ms, **kwargs):
            seen.append(at_ms)
            return b'\xff\xd8'

        with patched_cloudflare(frame=record):
            self.post_swift(duration_ms=60_000)
        self.assertLessEqual(len(seen), 12)


# ─── Requirement 10: caption and video are both consulted ─────────────────────
@override_settings(**AI_ON)
class CaptionAndVideoTests(AIModerationTestCase):
    #: Matches `moderation.text._FLAG_SPACED['self_harm.instruction']`. Flagged,
    #: not blocked, so the write is accepted and the row stored quarantined.
    FLAGGED_CAPTION = 'kill yourself'
    #: Matches a BLOCK rule, so the write itself is refused.
    BLOCKED_CAPTION = 'child porn'

    def test_a_safe_video_with_a_flagged_caption_is_quarantined(self):
        with patched_cloudflare():
            response = self.post_swift(caption=self.FLAGGED_CAPTION)
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_a_blocked_caption_is_still_refused_outright(self):
        """The existing text policy is untouched. Publication already happened
        is not a remedy, so this material never reaches a row at all."""
        with patched_cloudflare():
            response = self.post_swift(caption=self.BLOCKED_CAPTION)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'objectionable_content')
        self.assertFalse(SwiftVideo.objects.exists())

    def test_a_flagged_caption_spends_no_ai_budget(self):
        """It is already destined for the queue on a rule that has been
        decided; paying a vision model to agree is waste."""
        calls = []

        def record(source_url, at_ms, **kwargs):
            calls.append(at_ms)
            return b'\xff\xd8'

        with patched_cloudflare(frame=record):
            self.post_swift(caption=self.FLAGGED_CAPTION)
        self.assertEqual(calls, [])

    def test_an_unsafe_video_with_a_blameless_caption_is_quarantined(self):
        with patched_cloudflare(classify=unsafe_classifier(['nudity'], 0.9)):
            response = self.post_swift(caption='sunset at the lake')
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)

    def test_a_safe_video_and_a_safe_caption_publish(self):
        with patched_cloudflare():
            response = self.post_swift(caption='sunset at the lake')
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.OK)

    def test_the_overlay_text_is_screened_too(self):
        with patched_cloudflare():
            response = self.post_swift(overlay_text=self.FLAGGED_CAPTION)
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)


# ─── Requirement 9: audio ─────────────────────────────────────────────────────
@override_settings(**AI_ON, SWIFT_AI_AUDIO_MODERATION_ENABLED=True)
class AudioModerationTests(AIModerationTestCase):
    def test_a_prohibited_transcript_quarantines_a_visually_clean_swift(self):
        """The transcript goes through the *same* text classifier the caption
        does — a spoken slur and a typed one are one rule, not two."""
        with patched_cloudflare(
                transcribe=lambda data: TranscriptResult('you should kill yourself')):
            response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        job = SwiftModerationJob.objects.get(swift=swift)
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertEqual(job.basis, policy.TRANSCRIPT)
        self.assertTrue(job.audio_checked)

    def test_a_clean_transcript_publishes(self):
        with patched_cloudflare(
                transcribe=lambda data: TranscriptResult('what a nice day')):
            response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.OK)

    def test_audio_failure_does_not_hold_back_a_clean_video(self):
        """Optional means optional. The transcript is a second opinion on a
        different channel, not a second gate — letting it fail the run would
        make the optional half able to break the mandatory half."""
        def broken(url, duration_s=60):
            raise CloudflareError('no audio track', retryable=True)

        with patched_cloudflare(audio=broken):
            response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        job = SwiftModerationJob.objects.get(swift=swift)
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertFalse(job.audio_checked)

    @override_settings(SWIFT_AI_AUDIO_MODERATION_ENABLED=False)
    def test_audio_is_not_fetched_when_the_switch_is_off(self):
        calls = []

        with patched_cloudflare(audio=lambda url, duration_s=60: calls.append(1) or b''):
            self.post_swift()
        self.assertEqual(calls, [])


# ─── Requirement 6: the AI lands in the existing queue ────────────────────────
@override_settings(**AI_ON)
class ModerationQueueTests(AIModerationTestCase):
    def quarantine(self):
        with patched_cloudflare(classify=unsafe_classifier(['sexual_content'], 0.92)):
            response = self.post_swift()
        return SwiftVideo.objects.get(pk=response.json()['id'])

    def test_a_flagged_swift_gets_a_report_in_the_existing_table(self):
        """No second dashboard. It lands in `SwiftReportAdmin` next to the ones
        people filed and is closed by the same four buttons."""
        swift = self.quarantine()
        report = SwiftReport.objects.get(swift=swift)
        self.assertEqual(report.source, SwiftReport.Source.AI_VIDEO)
        self.assertEqual(report.reason, SwiftReport.Reason.NUDITY)
        self.assertEqual(report.state, SwiftReport.State.OPEN)

    def test_the_report_note_carries_evidence_but_no_prompt(self):
        swift = self.quarantine()
        note = SwiftReport.objects.get(swift=swift).note
        self.assertIn('sexual_content', note)
        self.assertIn('confidence', note)
        self.assertNotIn('classifier', note.lower())
        self.assertNotIn(FAKE_TOKEN, note)

    def test_the_report_records_the_strongest_model_claim_for_staff_only(self):
        timestamps = policy.sample_timestamps(15_000, policy.frame_count_for(15_000))
        strongest_at = timestamps[1]
        claim = 'A visible wound is bleeding.'

        def classify(image_bytes, at_ms=None, prompt=None):
            if at_ms == strongest_at:
                return ModerationResult(
                    safe=False, confidence=0.93, categories=('graphic_violence',),
                    at_ms=at_ms, evidence=claim)
            return ModerationResult(
                safe=False, confidence=0.3, categories=('weapons_threat',),
                at_ms=at_ms, evidence='A distant person may hold a weapon.')

        with patched_cloudflare(classify=classify):
            response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        note = SwiftReport.objects.get(swift=swift).note
        self.assertIn(f'Model claim at {strongest_at}ms: {claim}', note)
        self.assertNotIn('A distant person', note)
        self.assertIn('confidence 0.93', note)
        self.assertIn('8/8 frames at', note)
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertNotIn(claim, str(response.data))
        detail = self.client.get(reverse('swift-detail', args=[swift.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(claim, str(detail.data))

    def test_model_claim_in_report_is_redacted_and_bounded(self):
        def classify(image_bytes, at_ms=None, prompt=None):
            return ModerationResult(
                safe=False, confidence=0.92, categories=('sexual_content',),
                at_ms=at_ms,
                evidence=(f'Bearer other-secret-token {FAKE_TOKEN}\n'
                          + 'claimed observation ' * 40))

        with patched_cloudflare(classify=classify):
            response = self.post_swift()
        note = SwiftReport.objects.get(swift_id=response.json()['id']).note
        self.assertIn('Model claim at ', note)
        self.assertIn('Bearer [redacted] [redacted]', note)
        self.assertIn('claimed observation', note)
        self.assertNotIn('other-secret-token', note)
        self.assertNotIn(FAKE_TOKEN, note)
        self.assertNotIn('\n', note)
        self.assertLessEqual(len(note), 500)
        self.assertIn('sexual_content', note)
        self.assertIn('8/8 frames at', note)

    def test_the_job_row_holds_the_full_evidence(self):
        swift = self.quarantine()
        job = SwiftModerationJob.objects.get(swift=swift)
        self.assertEqual(job.categories, 'sexual_content')
        self.assertAlmostEqual(job.confidence, 0.92)
        self.assertTrue(job.sampled_timestamps)
        self.assertTrue(job.model_used)

    def test_an_enforcement_action_is_recorded_with_no_moderator(self):
        from moderation.models import ModerationAction

        swift = self.quarantine()
        action = ModerationAction.objects.filter(subject=self.creator).first()
        self.assertIsNotNone(action)
        self.assertIsNone(action.moderator)
        self.assertIn(str(swift.pk), action.detail)

    def test_a_retry_files_no_second_report(self):
        """The property the unique (swift, reporter) constraint underwrites.
        Two runs of the same job produce one queue item."""
        swift = self.quarantine()
        job = SwiftModerationJob.objects.get(swift=swift)
        job.state = SwiftModerationJob.State.QUEUED
        job.save(update_fields=['state'])

        with patched_cloudflare(classify=unsafe_classifier(['sexual_content'], 0.92)):
            ai_moderation.run(job.pk)

        self.assertEqual(SwiftReport.objects.filter(swift=swift).count(), 1)

    def test_a_moderator_restoring_it_publishes_it(self):
        swift = self.quarantine()
        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.OK)
        self.assertIn(swift.pk, self.feed_ids())


# ─── Requirement 15: a late verdict cannot overturn a human ───────────────────
@override_settings(**AI_ON)
class RaceTests(AIModerationTestCase):
    def test_a_late_safe_result_cannot_restore_a_removed_swift(self):
        """The race nobody can prevent, made harmless by a conditional UPDATE.
        A moderator's decision is not overturned by a job finishing after it."""
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        job = SwiftModerationJob.objects.create(swift=swift)

        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.REMOVED)

        with patched_cloudflare():
            ai_moderation.run(job.pk)

        swift.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.REMOVED)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_a_late_unsafe_result_does_not_re_hide_a_restored_swift(self):
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        job = SwiftModerationJob.objects.create(swift=swift)
        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.OK)

        with patched_cloudflare(classify=unsafe_classifier(['nudity'], 0.9)):
            ai_moderation.run(job.pk)

        swift.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.OK)
        # The evidence is still filed, because the moderator who made the other
        # decision should be able to see what the classifier thought.
        self.assertTrue(SwiftReport.objects.filter(swift=swift).exists())

    def test_two_workers_cannot_claim_the_same_job(self):
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        job = SwiftModerationJob.objects.create(swift=swift)

        self.assertIsNotNone(ai_moderation.claim(job.pk))
        self.assertIsNone(ai_moderation.claim(job.pk))


# ─── Retry and the sweeper ────────────────────────────────────────────────────
@override_settings(**AI_ON)
class RetryTests(AIModerationTestCase):
    def failing_job(self):
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        return swift, SwiftModerationJob.objects.create(swift=swift)

    def boom(self, retryable=True):
        def frame(source_url, at_ms, **kwargs):
            raise CloudflareError('cloudflare is down', retryable=retryable)
        return frame


    def exhausted_job(self):
        swift, job = self.failing_job()
        job.attempts = 3
        job.save(update_fields=['attempts'])
        ai_moderation._fail(job, 'credentials unavailable', retryable=False)
        swift.refresh_from_db()
        job.refresh_from_db()
        return swift, job

    def test_failed_jobs_need_an_explicit_retry(self):
        swift, job = self.exhausted_job()
        with patch.object(ai_moderation, '_classify') as classify:
            self.assertEqual(ai_moderation.sweep(), 0)
        classify.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.FAILED)

    def test_retry_resets_only_selected_failed_job_and_keeps_it_private(self):
        swift, job = self.exhausted_job()
        other, untouched = self.exhausted_job()
        self.assertEqual(ai_moderation.retry_failed([swift.pk, swift.pk]), [job.pk])
        swift.refresh_from_db()
        job.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertNotIn(swift.pk, self.feed_ids())
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.next_attempt_at)
        self.assertIsNone(job.finished_at)
        self.assertEqual(job.last_error, '')
        self.assertEqual(untouched.state, SwiftModerationJob.State.FAILED)
        self.assertEqual(ai_moderation.retry_failed([swift.pk]), [])

    def test_retry_handles_structured_results_through_the_real_client(self):
        from moderation.test_cloudflare_ai import ai_ok, vision_payload

        for unsafe in (False, True):
            with self.subTest(unsafe=unsafe):
                swift, job = self.exhausted_job()
                ai_moderation.retry_failed([swift.pk])
                response = ai_ok(vision_payload(unsafe, confidence=0.99))
                with patch.object(cloudflare_ai, 'fetch_frame', return_value=b'jpeg'), \
                        patch.object(cloudflare_ai.requests, 'post', return_value=response):
                    ai_moderation.run(job.pk)
                swift.refresh_from_db()
                job.refresh_from_db()
                self.assertEqual(job.state, SwiftModerationJob.State.DONE)
                self.assertGreater(job.frames_requested, 0)
                self.assertEqual(job.frames_classified, job.frames_requested)
                self.assertEqual(
                    swift.moderation, Moderation.QUARANTINED if unsafe else Moderation.OK)
                self.assertEqual(swift.pk in self.feed_ids(), not unsafe)
                self.assertEqual(
                    job.report.state,
                    SwiftReport.State.OPEN if unsafe else SwiftReport.State.REVIEWED,
                )

    def test_unexplained_or_contradictory_flags_keep_retried_uploads_hidden(self):
        from moderation.test_cloudflare_ai import ai_ok, vision_payload

        for response in (
            {'unsafe': True, 'categories': ['other_objectionable'], 'confidence': 0.8},
            vision_payload(True, categories=['other_objectionable'], evidence='No visible violation'),
        ):
            with self.subTest(response=response):
                swift, job = self.exhausted_job()
                ai_moderation.retry_failed([swift.pk])
                with patch.object(cloudflare_ai, 'fetch_frame', return_value=b'jpeg'), \
                        patch.object(cloudflare_ai.requests, 'post', return_value=ai_ok(response)):
                    ai_moderation.run(job.pk)
                swift.refresh_from_db()
                job.refresh_from_db()
                self.assertEqual(swift.moderation, Moderation.PENDING)
                self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
                self.assertNotIn(swift.pk, self.feed_ids())

    def test_invalid_structured_results_keep_retried_uploads_hidden(self):
        from moderation.test_cloudflare_ai import ai_ok

        swift, job = self.exhausted_job()
        ai_moderation.retry_failed([swift.pk])
        response = ai_ok({'unsafe': 'unknown', 'confidence': 0.99})
        with patch.object(cloudflare_ai, 'fetch_frame', return_value=b'jpeg'), \
                patch.object(cloudflare_ai.requests, 'post', return_value=response):
            ai_moderation.run(job.pk)
        swift.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.PENDING)
        self.assertNotIn(swift.pk, self.feed_ids())
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertIsNotNone(job.next_attempt_at)
        self.assertNotIn('TypeError', job.last_error)
        self.assertEqual(job.report.state, SwiftReport.State.OPEN)

    def test_retry_approval_publishes_and_closes_failure_report(self):
        swift, job = self.exhausted_job()
        report_id = job.report_id
        ai_moderation.retry_failed([swift.pk])
        with patched_cloudflare():
            ai_moderation.run(job.pk)
        swift.refresh_from_db()
        report = SwiftReport.objects.get(pk=report_id)
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertIn(swift.pk, self.feed_ids())
        self.assertEqual(report.state, SwiftReport.State.REVIEWED)
        self.assertIsNotNone(report.reviewed_at)
        self.assertIsNone(report.reviewed_by)
        self.assertEqual(SwiftReport.objects.filter(swift=swift).count(), 1)

    def test_unsafe_retry_reuses_report_with_current_evidence(self):
        swift, job = self.exhausted_job()
        report_id = job.report_id
        ai_moderation.retry_failed([swift.pk])
        with patched_cloudflare(classify=unsafe_classifier(['sexual_content'], 0.92)):
            ai_moderation.run(job.pk)
        swift.refresh_from_db()
        job.refresh_from_db()
        report = SwiftReport.objects.get(pk=report_id)
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertEqual(job.report_id, report_id)
        self.assertEqual(report.state, SwiftReport.State.OPEN)
        self.assertEqual(report.reason, SwiftReport.Reason.NUDITY)
        self.assertIn('sexual_content', report.note)
        self.assertNotIn(policy.INSUFFICIENT_FRAMES, report.note)
        self.assertEqual(SwiftReport.objects.filter(swift=swift).count(), 1)
        self.assertEqual(ai_moderation.retry_failed([swift.pk]), [])

    @override_settings(SWIFT_AI_MAX_ATTEMPTS=2)
    def test_explicit_retry_has_a_fresh_automatic_retry_budget(self):
        swift, job = self.exhausted_job()
        ai_moderation.retry_failed([swift.pk])
        with patched_cloudflare(frame=self.boom()):
            ai_moderation.run(job.pk)
        job.refresh_from_db()
        swift.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertIsNotNone(job.next_attempt_at)
        self.assertEqual(swift.moderation, Moderation.PENDING)

    def test_retry_skips_removed_restored_and_deleted_swifts(self):
        from django.utils import timezone

        for changes in (
            {'moderation': Moderation.REMOVED}, {'moderation': Moderation.OK},
            {'moderation': Moderation.FLAGGED}, {'deleted_at': timezone.now()},
            {'video': ''},
        ):
            with self.subTest(changes=changes):
                swift, job = self.exhausted_job()
                SwiftVideo.objects.filter(pk=swift.pk).update(**changes)
                self.assertEqual(ai_moderation.retry_failed([swift.pk]), [])
                job.refresh_from_db()
                self.assertEqual(job.state, SwiftModerationJob.State.FAILED)
                swift.refresh_from_db()
                for field, value in changes.items():
                    self.assertEqual(getattr(swift, field), value)

    def test_retry_skips_reviewed_or_unrelated_reports(self):
        from django.utils import timezone

        for changes in (
            {'state': SwiftReport.State.REVIEWED},
            {'state': SwiftReport.State.ACTIONED},
            {'reviewed_at': timezone.now()},
            {'reviewed_by': self.stranger},
            {'action_taken': 'user_warned'},
            {'moderator_note': 'Leave for manual review'},
            {'source': SwiftReport.Source.REPORT},
        ):
            with self.subTest(changes=changes):
                swift, job = self.exhausted_job()
                SwiftReport.objects.filter(pk=job.report_id).update(**changes)
                self.assertEqual(ai_moderation.retry_failed([swift.pk]), [])
                swift.refresh_from_db()
                self.assertEqual(swift.moderation, Moderation.QUARANTINED)

    def test_retry_requires_its_failure_report_and_no_independent_reports(self):
        for independent in (False, True):
            with self.subTest(independent=independent):
                swift, job = self.exhausted_job()
                if independent:
                    SwiftReport.objects.create(
                        swift=swift, reporter=self.stranger, reason='spam')
                else:
                    SwiftModerationJob.objects.filter(pk=job.pk).update(report=None)
                self.assertEqual(ai_moderation.retry_failed([swift.pk]), [])

    def test_retry_does_not_undo_a_later_moderator_removal(self):
        from django.utils import timezone

        swift, job = self.exhausted_job()
        ai_moderation.retry_failed([swift.pk])
        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.REMOVED)
        SwiftReport.objects.filter(pk=job.report_id).update(
            state=SwiftReport.State.ACTIONED, reviewed_by=self.stranger,
            reviewed_at=timezone.now(), action_taken='content_removed',
            moderator_note='Removed after review',
        )
        with patched_cloudflare():
            ai_moderation.run(job.pk)
        swift.refresh_from_db()
        report = SwiftReport.objects.get(pk=job.report_id)
        self.assertEqual(swift.moderation, Moderation.REMOVED)
        self.assertEqual(report.state, SwiftReport.State.ACTIONED)
        self.assertEqual(report.moderator_note, 'Removed after review')

    def test_retry_command_processes_only_selected_swifts(self):
        from io import StringIO
        from django.core.management import call_command

        swift, job = self.exhausted_job()
        other, untouched = self.failing_job()
        out = StringIO()
        with patched_cloudflare():
            call_command('moderate_swifts', '--retry-failed', str(swift.pk), stdout=out)
        swift.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertEqual(untouched.state, SwiftModerationJob.State.QUEUED)
        self.assertEqual(untouched.attempts, 0)
        self.assertIn('Requeued 1 failed job(s); skipped 0', out.getvalue())
        self.assertIn(f'Swift {swift.pk}: done', out.getvalue())

    def test_a_retryable_failure_reschedules(self):
        swift, job = self.failing_job()
        with patched_cloudflare(frame=self.boom()):
            ai_moderation.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.next_attempt_at)

    def test_a_non_retryable_failure_gives_up_at_once(self):
        """Retrying a 403 for a token without Workers AI permission burns quota
        to be told the same thing three times."""
        swift, job = self.failing_job()
        with patched_cloudflare(frame=self.boom(retryable=False)):
            ai_moderation.run(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.FAILED)

    @override_settings(SWIFT_AI_MAX_ATTEMPTS=2)
    def test_running_out_of_attempts_quarantines_for_a_human(self):
        """Not a finding that the content is bad — a finding that the pipeline
        could not answer. Leaving it PENDING forever would put it in no queue
        and tell the author nothing."""
        swift, job = self.failing_job()
        for _ in range(3):
            job.refresh_from_db()
            if job.state == SwiftModerationJob.State.QUEUED:
                job.next_attempt_at = None
                job.save(update_fields=['next_attempt_at'])
            with patched_cloudflare(frame=self.boom()):
                ai_moderation.run(job.pk)

        job.refresh_from_db()
        swift.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.FAILED)
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertTrue(SwiftReport.objects.filter(
            swift=swift, source=SwiftReport.Source.AI_VIDEO).exists())

    def test_the_sweeper_picks_up_a_job_whose_thread_never_ran(self):
        """The property that makes this feature correct rather than merely
        fast: losing the process loses latency, not work."""
        swift, job = self.failing_job()
        with patched_cloudflare():
            self.assertEqual(ai_moderation.sweep(), 1)
        swift.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.OK)

    def test_the_sweeper_recovers_a_job_stuck_in_running(self):
        from datetime import timedelta

        from django.utils import timezone

        swift, job = self.failing_job()
        SwiftModerationJob.objects.filter(pk=job.pk).update(
            state=SwiftModerationJob.State.RUNNING,
            updated_at=timezone.now() - timedelta(hours=2),
        )
        with patched_cloudflare():
            self.assertEqual(ai_moderation.sweep(), 1)
        swift.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.OK)

    def test_the_sweeper_leaves_a_backed_off_job_alone(self):
        swift, job = self.failing_job()
        with patched_cloudflare(frame=self.boom()):
            ai_moderation.run(job.pk)
        with patched_cloudflare():
            self.assertEqual(ai_moderation.sweep(), 0)
        swift.refresh_from_db()
        self.assertEqual(swift.moderation, Moderation.PENDING)


# ─── Requirement 16: existing content is untouched ────────────────────────────
@override_settings(**AI_ON)
class ExistingContentTests(AIModerationTestCase):
    def test_already_published_swifts_are_not_re_moderated(self):
        published = make_swift(self.creator)
        self.assertEqual(published.moderation, Moderation.OK)
        with patched_cloudflare(classify=unsafe_classifier(['nudity'], 1.0)):
            ai_moderation.sweep()
        published.refresh_from_db()
        self.assertEqual(published.moderation, Moderation.OK)
        self.assertFalse(SwiftModerationJob.objects.exists())

    def test_a_swift_with_no_uploaded_file_gets_no_job(self):
        """Every legacy Cloudinary-era row. There is nothing at a URL for
        Cloudflare to fetch, so there is nothing to run."""
        legacy = make_swift(self.creator, moderation=Moderation.PENDING)
        self.assertIsNone(ai_moderation.enqueue(legacy))

    def test_reports_on_existing_swifts_withhold_them_until_review(self):
        published = make_swift(self.creator)
        self.other.post(
            reverse('swift-report', args=[published.pk]), {'reason': 'spam'})
        published.refresh_from_db()
        self.assertEqual(published.moderation, Moderation.FLAGGED)
        self.assertFalse(published.is_live)


# ─── The switch ───────────────────────────────────────────────────────────────
class SwitchedOffTests(AIModerationTestCase):
    """With the feature off, a Swift is published exactly as it was before."""

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_a_swift_publishes_immediately_when_moderation_is_off(self):
        response = self.post_swift()
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertTrue(swift.is_live)
        self.assertFalse(SwiftModerationJob.objects.exists())

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_caption_screening_still_works_with_the_feature_off(self):
        response = self.post_swift(caption='kill yourself')
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)


# ─── Blocking and reporting still behave ──────────────────────────────────────
@override_settings(**AI_ON)
class VisibilityRegressionTests(AIModerationTestCase):
    def test_a_blocked_author_is_still_invisible(self):
        block(self.stranger, self.creator)
        with patched_cloudflare():
            response = self.post_swift()
        swift_id = response.json()['id']
        self.assertNotIn(swift_id, self.feed_ids())
        self.assertEqual(
            self.other.get(reverse('swift-detail', args=[swift_id])).status_code, 404)

    def test_a_friends_only_swift_still_reaches_only_friends(self):
        with patched_cloudflare():
            response = self.post_swift(visibility='friends')
        swift_id = response.json()['id']
        self.assertNotIn(swift_id, self.feed_ids())
        befriend(self.creator, self.stranger)
        self.assertIn(swift_id, self.feed_ids())


# ─── Requirement 12: nothing leaks ────────────────────────────────────────────
@override_settings(**AI_ON)
class NoLeakTests(AIModerationTestCase):
    def test_the_token_appears_in_no_api_response(self):
        with patched_cloudflare():
            created = self.post_swift()
        bodies = [
            created.content.decode(),
            self.client.get(reverse('swift-mine')).content.decode(),
            self.other.get(reverse('swift-feed')).content.decode(),
            self.client.get(
                reverse('swift-detail', args=[created.json()['id']])
            ).content.decode(),
        ]
        for body in bodies:
            self.assertNotIn(FAKE_TOKEN, body)
            self.assertNotIn('CLOUDFLARE', body)
            self.assertNotIn('acct-test-0000', body)

    def test_no_confidence_score_reaches_a_normal_user(self):
        """A confidence score published to the author is a free tuning signal
        for anybody trying to get material past the filter."""
        with patched_cloudflare(classify=unsafe_classifier(['nudity'], 0.91)):
            created = self.post_swift()
        body = self.client.get(
            reverse('swift-detail', args=[created.json()['id']])).json()
        self.assertEqual(body['review_state'], 'needs_review')
        self.assertNotIn('confidence', body)
        self.assertNotIn('categories', body)
        self.assertNotIn('0.91', str(body))

    def test_the_token_appears_in_no_log_line(self):
        with self.assertLogs('swifts.ai_moderation', level=logging.DEBUG) as logs:
            logging.getLogger('swifts.ai_moderation').debug('probe')
            def boom(source_url, at_ms, **kwargs):
                raise CloudflareError(
                    f'HTTP 401: Bearer {FAKE_TOKEN}', retryable=True)
            with patched_cloudflare(frame=boom):
                self.post_swift()
        self.assertNotIn(FAKE_TOKEN, '\n'.join(logs.output))


@override_settings(**dict(AI_ON, SWIFT_AI_FRAME_COUNT=5,
                          SWIFT_AI_AUDIO_MODERATION_ENABLED=False))
class MixedVisionResponseTests(AIModerationTestCase):
    """A malformed verdict cannot disappear among otherwise safe samples."""

    def run_responses(self, responses, *, frame_results=None):
        from moderation.test_cloudflare_ai import ai_ok

        swift = make_swift(
            self.creator, moderation=Moderation.PENDING,
            video=fake_video(), duration_ms=8_000)
        job = SwiftModerationJob.objects.create(swift=swift)
        with patch.object(cloudflare_ai, 'fetch_frame',
                          side_effect=frame_results or [b'jpeg'] * 5) as fetch, \
                patch.object(cloudflare_ai.requests, 'post',
                             side_effect=[ai_ok(answer) for answer in responses]) as post:
            ai_moderation.run(job.pk)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(post.call_count, len(responses))
        swift.refresh_from_db()
        job.refresh_from_db()
        return swift, job

    def test_four_safe_frames_do_not_override_one_invalid_object_answer(self):
        from moderation.test_cloudflare_ai import vision_payload

        invalid_answers = (
            vision_payload(True, evidence=''),
            vision_payload(False, categories=['nudity']),
            {'unsafe': False, 'categories': [], 'confidence': 0.97},
            None,
            [],
            True,
            1,
            '',
            '   ',
        )
        for invalid in invalid_answers:
            with self.subTest(invalid=invalid):
                swift, job = self.run_responses([vision_payload()] * 4 + [invalid])
                self.assertEqual(swift.moderation, Moderation.PENDING)
                self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
                self.assertIsNotNone(job.next_attempt_at)
                self.assertEqual(job.attempts, 1)
                self.assertNotIn(swift.pk, self.feed_ids())
                self.assertFalse(SwiftReport.objects.filter(swift=swift).exists())

    def test_valid_unsafe_frame_still_quarantines_beside_an_invalid_answer(self):
        from moderation.test_cloudflare_ai import vision_payload

        swift, job = self.run_responses(
            [vision_payload()] * 3
            + [vision_payload(True, evidence=''), vision_payload(True)])
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertEqual(job.state, SwiftModerationJob.State.DONE)
        self.assertEqual(job.outcome, policy.QUARANTINED)
        self.assertEqual(job.frames_requested, 5)
        self.assertEqual(job.frames_classified, 4)
        self.assertEqual(job.report.reason, SwiftReport.Reason.NUDITY)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_one_frame_fetch_failure_does_not_block_four_valid_safe_frames(self):
        from moderation.test_cloudflare_ai import vision_payload

        swift, job = self.run_responses(
            [vision_payload()] * 4,
            frame_results=[b'jpeg'] * 4
            + [CloudflareError('Seek time exceeds media duration.', retryable=False)])
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertEqual(job.state, SwiftModerationJob.State.DONE)
        self.assertEqual(job.frames_requested, 5)
        self.assertEqual(job.frames_classified, 4)
        self.assertIn(swift.pk, self.feed_ids())
