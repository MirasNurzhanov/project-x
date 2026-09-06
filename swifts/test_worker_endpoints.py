"""The contract the Go moderation worker depends on.

These are the tests that matter most in this integration, because the worker
lives in another language and cannot be type-checked against Django. If the
lease payload loses a field or the settle path stops applying a decision, the
only thing that notices is this file.

The security assertions are the ones worth reading twice. A worker that could
send `{"approved": true}` would be a way to publish a Swift without
classifying it; the payload has no such field, and
`test_a_worker_cannot_publish_a_swift_by_asserting_it_is_safe` is what keeps it
that way.
"""
import json

from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from moderation.reasons import Moderation
from swifts.models import SwiftModerationJob, SwiftReport, SwiftVideo
from swifts.test_ai_moderation import AIModerationTestCase, AI_ON, patched_cloudflare
from swifts.testing import fake_video, make_swift

TOKEN = 'worker-token-NEVER-REAL'

WORKER = dict(AI_ON, MODERATION_WORKER_TOKEN=TOKEN)


@override_settings(**WORKER)
class LeaseEndpointTests(AIModerationTestCase):
    def setUp(self):
        super().setUp()
        self.worker = APIClient()
        self.worker.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}',
                                HTTP_X_MODERATION_VISION_FORMATS='image,messages')

    def pending_job(self, **extra):
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video(), **extra)
        return SwiftModerationJob.objects.create(swift=swift), swift

    def lease(self, limit=4):
        return self.worker.get(reverse('worker-lease'), {'limit': limit})

    # ── Access ───────────────────────────────────────────────────────────────
    def test_an_unauthenticated_caller_gets_nothing(self):
        anon = APIClient()
        self.assertEqual(anon.get(reverse('worker-lease')).status_code, 403)

    def test_a_wrong_token_gets_nothing(self):
        wrong = APIClient()
        wrong.credentials(HTTP_AUTHORIZATION='Bearer not-the-token')
        self.assertEqual(wrong.get(reverse('worker-lease')).status_code, 403)

    @override_settings(MODERATION_WORKER_TOKEN='')
    def test_an_unset_token_closes_the_endpoint_rather_than_opening_it(self):
        """The direction of this default is the whole point. "Unset disables
        the check" would let anything on the network forge a verdict."""
        self.assertEqual(self.lease().status_code, 403)

    def test_an_ordinary_signed_in_user_cannot_lease(self):
        """These are machine endpoints. A phone's JWT must not reach them."""
        self.assertEqual(self.client.get(reverse('worker-lease')).status_code, 403)

    # ── The payload ──────────────────────────────────────────────────────────
    def test_a_lease_carries_everything_the_worker_needs(self):
        job, swift = self.pending_job()
        body = self.lease().json()

        self.assertEqual(len(body['leases']), 1)
        lease = body['leases'][0]
        for field in ('job_id', 'swift_id', 'source_url', 'timestamps_ms',
                      'duration_ms', 'want_audio', 'frame_width', 'prompt',
                      'vision_model', 'transcribe_model', 'vision_response_format',
                      'vision_max_tokens', 'vision_input_format'):
            self.assertIn(field, lease, f'lease is missing {field}')
        self.assertEqual(lease['job_id'], job.pk)
        self.assertGreaterEqual(len(lease['timestamps_ms']), 2)
        from moderation import cloudflare_ai

        self.assertEqual(lease['vision_response_format'], cloudflare_ai.VISION_RESPONSE_FORMAT)
        self.assertEqual(lease['vision_max_tokens'], cloudflare_ai.VISION_MAX_TOKENS)
        self.assertEqual(lease['vision_input_format'], cloudflare_ai.vision_input_format())

    def test_a_scout_lease_requests_image_messages_and_legacy_uses_byte_arrays(self):
        for model, input_format in (
            ('@cf/meta/llama-4-scout-17b-16e-instruct', 'messages'),
            ('@cf/meta/llama-3.2-11b-vision-instruct', 'image'),
        ):
            with self.subTest(model=model), override_settings(CLOUDFLARE_AI_VISION_MODEL=model):
                self.pending_job()
                lease = self.lease().json()['leases'][0]
                self.assertEqual(lease['vision_model'], model)
                self.assertEqual(lease['vision_input_format'], input_format)

    def test_leasing_claims_the_job(self):
        """A leased job is RUNNING, so nothing else picks it up — the same
        atomic claim the in-process thread and the sweeper race through."""
        job, _ = self.pending_job()
        self.lease()
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.RUNNING)
        self.assertEqual(job.attempts, 1)

    def test_old_workers_cannot_claim_scout_jobs_but_can_still_lease_legacy_jobs(self):
        old_worker = APIClient()
        old_worker.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')
        job, _ = self.pending_job()
        with override_settings(CLOUDFLARE_AI_VISION_MODEL='@cf/meta/llama-4-scout-17b-16e-instruct'):
            response = old_worker.get(reverse('worker-lease'))
        self.assertEqual(response.status_code, 409)
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
        self.assertEqual(job.attempts, 0)
        with override_settings(CLOUDFLARE_AI_VISION_MODEL='@cf/meta/llama-3.2-11b-vision-instruct'):
            response = old_worker.get(reverse('worker-lease'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['leases'][0]['job_id'], job.pk)

    def test_trimmed_leases_sample_the_visible_source_range(self):
        for metadata in (
            {'duration_ms': 30_000, 'trim_end_ms': 18_000},
            {'duration_ms': 18_000},
        ):
            with self.subTest(metadata=metadata):
                job, swift = self.pending_job(trim_start_ms=10_000, **metadata)
                lease = self.lease().json()['leases'][0]
                self.assertEqual(lease['job_id'], job.pk)
                self.assertEqual(lease['source_url'], swift.video.url)
                self.assertEqual(lease['duration_ms'], 8_000)
                self.assertEqual(
                    lease['timestamps_ms'], [10_400, 12_200, 14_000, 15_800, 17_600])

    def test_a_job_is_leased_to_exactly_one_caller(self):
        self.pending_job()
        first = self.lease().json()['leases']
        second = self.lease().json()['leases']
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0, 'the same job was leased twice')

    def test_limit_is_respected_and_capped(self):
        for _ in range(6):
            self.pending_job()
        self.assertEqual(len(self.lease(limit=2).json()['leases']), 2)

    def test_no_work_is_an_empty_list_not_an_error(self):
        body = self.lease()
        self.assertEqual(body.status_code, 200)
        self.assertEqual(body.json()['leases'], [])

    def test_a_swift_with_no_media_is_failed_rather_than_leased(self):
        """Leasing work that cannot be done would have the worker fetch a
        nonexistent URL and report a failure a round trip later."""
        swift = make_swift(self.creator, moderation=Moderation.PENDING)
        SwiftVideo.objects.filter(pk=swift.pk).update(video='', video_url='')
        job = SwiftModerationJob.objects.create(swift=swift)

        self.assertEqual(self.lease().json()['leases'], [])
        job.refresh_from_db()
        self.assertNotEqual(job.state, SwiftModerationJob.State.RUNNING)


@override_settings(**WORKER)
class SettleEndpointTests(AIModerationTestCase):
    def setUp(self):
        super().setUp()
        self.worker = APIClient()
        self.worker.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}',
                                HTTP_X_MODERATION_VISION_FORMATS='image,messages')
        self.swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        self.job = SwiftModerationJob.objects.create(swift=self.swift)
        self.lease = self.worker.get(reverse('worker-lease')).json()['leases'][0]

    def settle(self, **payload):
        body = {'job_id': self.job.pk, 'model_used': '@cf/test/model', **payload}
        return self.worker.post(
            reverse('worker-settle'), json.dumps(body),
            content_type='application/json')

    def frames(self, raw, count=None):
        stamps = self.lease['timestamps_ms']
        if count is not None:
            stamps = stamps[:count]
        return [{'at_ms': ms, 'raw': raw} for ms in stamps]

    SAFE = json.dumps({
        'description': 'A desk and laptop.', 'evidence': '',
        'unsafe': False, 'categories': [], 'confidence': 0.97,
    })
    UNSAFE = json.dumps({
        'description': 'Explicit imagery.', 'evidence': 'Nudity: intimate body parts are exposed.',
        'unsafe': True, 'categories': ['nudity'], 'confidence': 0.95,
    })

    # ── The happy paths ──────────────────────────────────────────────────────
    def test_all_safe_frames_publish_the_swift(self):
        response = self.settle(frames=self.frames(self.SAFE))
        self.assertEqual(response.status_code, 200, response.data)

        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.OK)
        self.assertTrue(self.swift.is_live)

    def test_an_unsafe_frame_quarantines_and_files_a_report(self):
        self.settle(frames=self.frames(self.UNSAFE))

        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.QUARANTINED)
        report = SwiftReport.objects.get(swift=self.swift)
        self.assertEqual(report.source, SwiftReport.Source.AI_VIDEO)

    def test_the_job_records_what_the_worker_reported(self):
        self.settle(frames=self.frames(self.SAFE), audio_checked=True)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, SwiftModerationJob.State.DONE)
        self.assertEqual(self.job.model_used, '@cf/test/model')
        self.assertTrue(self.job.audio_checked)
        self.assertTrue(self.job.sampled_timestamps)

    # ── Django owns the decision, not the worker ─────────────────────────────
    def test_a_worker_cannot_publish_a_swift_by_asserting_it_is_safe(self):
        """The security property of the whole design.

        There is no verdict field in the contract. A worker that reports no
        classified frames gets the *failure* path, not an approval — so the
        worst a compromised worker can do is delay a Swift, never publish one.
        """
        response = self.settle(
            frames=[], approved=True, outcome='approved', safe=True)
        self.assertEqual(response.status_code, 200)

        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)

    def test_unreadable_model_output_does_not_count_as_a_clean_frame(self):
        """A broken model must not silently approve everything. Frames that
        cannot be parsed are counted as requested-but-not-classified, which is
        what makes `combine_frames` refuse to approve."""
        self.settle(frames=self.frames('I cannot help with that.'))

        self.swift.refresh_from_db()
        self.assertNotEqual(self.swift.moderation, Moderation.OK)

    def test_too_few_classified_frames_is_a_retry_not_an_approval(self):
        stamps = self.lease['timestamps_ms']
        frames = [{'at_ms': stamps[0], 'raw': self.SAFE}]
        frames += [{'at_ms': ms, 'error': 'fetch failed'} for ms in stamps[1:]]
        self.settle(frames=frames)

        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, SwiftModerationJob.State.QUEUED)

    def test_a_legacy_safe_answer_without_evidence_fields_is_not_published(self):
        self.settle(frames=self.frames('{"unsafe": false, "categories": [], "confidence": 0.97}'))
        self.swift.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)
        self.assertEqual(self.job.state, SwiftModerationJob.State.QUEUED)

    def test_an_unsafe_answer_with_no_visible_violation_is_retried(self):
        payload = json.loads(self.UNSAFE)
        payload['categories'] = ['other_objectionable']
        payload['evidence'] = 'No visible violation'
        self.settle(frames=self.frames(json.dumps(payload)))
        self.swift.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)
        self.assertEqual(self.job.state, SwiftModerationJob.State.QUEUED)

    def test_a_prohibited_transcript_quarantines_a_visually_clean_swift(self):
        """The transcript goes through Django's existing text classifier — the
        same one the caption goes through — rather than a second filter in Go."""
        self.settle(
            frames=self.frames(self.SAFE),
            transcript='you should kill yourself',
            audio_checked=True,
        )
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.QUARANTINED)

    # ── Failure reporting ────────────────────────────────────────────────────
    def test_a_reported_error_reschedules_the_job(self):
        self.settle(error='workers ai unreachable', retryable=True)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, SwiftModerationJob.State.QUEUED)
        self.assertIsNotNone(self.job.next_attempt_at)
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)

    def test_a_non_retryable_error_gives_up_at_once(self):
        self.settle(error='credentials are not configured', retryable=False)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, SwiftModerationJob.State.FAILED)

    def test_a_late_settle_for_a_reclaimed_job_is_ignored(self):
        """The race the stale sweep creates: a slow worker reports after its
        job was re-queued and picked up elsewhere. Applying it twice would file
        a second report; ignoring it is correct."""
        SwiftModerationJob.objects.filter(pk=self.job.pk).update(
            state=SwiftModerationJob.State.QUEUED)

        response = self.settle(frames=self.frames(self.SAFE))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['applied'])
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, Moderation.PENDING)

    def test_an_unknown_job_is_a_404(self):
        response = self.worker.post(
            reverse('worker-settle'), json.dumps({'job_id': 999999, 'frames': []}),
            content_type='application/json')
        self.assertEqual(response.status_code, 404)


@override_settings(**WORKER)
class CoexistenceTests(AIModerationTestCase):
    """The Go worker and the existing Python paths run side by side.

    Both lease through the same atomic claim, so this is not a new concurrency
    design — it is a third consumer of one that already had two.
    """

    def setUp(self):
        super().setUp()
        self.worker = APIClient()
        self.worker.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}',
                                HTTP_X_MODERATION_VISION_FORMATS='image,messages')

    def test_a_job_the_python_sweeper_took_is_not_leased_to_go(self):
        from swifts import ai_moderation

        swift = make_swift(
            self.creator, moderation=Moderation.PENDING, video=fake_video())
        SwiftModerationJob.objects.create(swift=swift)

        with patched_cloudflare():
            ai_moderation.sweep()

        self.assertEqual(
            self.worker.get(reverse('worker-lease')).json()['leases'], [],
            'the Go worker was handed a job Python had already finished')


@override_settings(**dict(WORKER, SWIFT_AI_FRAME_COUNT=5))
class MixedSettleResponseTests(AIModerationTestCase):
    """The Go settle path applies the same rule to incomplete verdicts."""

    FETCH_ERROR = object()

    def setUp(self):
        super().setUp()
        self.worker = APIClient()
        self.worker.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}',
                                HTTP_X_MODERATION_VISION_FORMATS='image,messages')

    def settle_responses(self, responses):
        swift = make_swift(
            self.creator, moderation=Moderation.PENDING,
            video=fake_video(), duration_ms=8_000)
        job = SwiftModerationJob.objects.create(swift=swift)
        lease = self.worker.get(reverse('worker-lease')).json()['leases'][0]
        self.assertEqual(lease['job_id'], job.pk)
        self.assertEqual(len(lease['timestamps_ms']), 5)
        self.assertEqual(len(responses), 5)
        frames = [
            {'at_ms': at_ms, 'raw': json.dumps(answer)} if answer is not self.FETCH_ERROR
            else {'at_ms': at_ms, 'error': 'Seek time exceeds media duration.'}
            for at_ms, answer in zip(lease['timestamps_ms'], responses)
        ]
        response = self.worker.post(
            reverse('worker-settle'),
            json.dumps({'job_id': job.pk, 'frames': frames, 'model_used': '@cf/test/model'}),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        swift.refresh_from_db()
        job.refresh_from_db()
        return swift, job, response.json()

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
                swift, job, response = self.settle_responses(
                    [vision_payload()] * 4 + [invalid])
                self.assertEqual(response['outcome'], 'failed')
                self.assertEqual(swift.moderation, Moderation.PENDING)
                self.assertEqual(job.state, SwiftModerationJob.State.QUEUED)
                self.assertIsNotNone(job.next_attempt_at)
                self.assertEqual(job.attempts, 1)
                self.assertNotIn(swift.pk, self.feed_ids())
                self.assertFalse(SwiftReport.objects.filter(swift=swift).exists())

    def test_valid_unsafe_frame_still_quarantines_beside_an_invalid_answer(self):
        from moderation.test_cloudflare_ai import vision_payload

        swift, job, response = self.settle_responses(
            [vision_payload()] * 3
            + [vision_payload(True, evidence=''), vision_payload(True)])
        self.assertEqual(response['outcome'], 'quarantined')
        self.assertEqual(swift.moderation, Moderation.QUARANTINED)
        self.assertEqual(job.state, SwiftModerationJob.State.DONE)
        self.assertEqual(job.frames_requested, 5)
        self.assertEqual(job.frames_classified, 4)
        self.assertEqual(job.report.reason, SwiftReport.Reason.NUDITY)
        self.assertNotIn(swift.pk, self.feed_ids())

    def test_one_frame_fetch_failure_does_not_block_four_valid_safe_frames(self):
        from moderation.test_cloudflare_ai import vision_payload

        swift, job, response = self.settle_responses([vision_payload()] * 4 + [self.FETCH_ERROR])
        self.assertEqual(response['outcome'], 'approved')
        self.assertEqual(swift.moderation, Moderation.OK)
        self.assertEqual(job.state, SwiftModerationJob.State.DONE)
        self.assertEqual(job.frames_requested, 5)
        self.assertEqual(job.frames_classified, 4)
        self.assertIn(swift.pk, self.feed_ids())
