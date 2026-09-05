"""The two endpoints `services/go-moderation` talks to.

Deliberately the smallest possible addition to Django. Neither view contains a
moderation rule: `lease` claims through the existing
`ai_moderation.claim`, and `settle` parses through the existing
`cloudflare_ai.parse_vision_response` and decides through the existing
`policy.combine_frames`. The Go worker does the Cloudflare calls and nothing
else.

That split is the whole design, and it is worth stating why it is drawn here
rather than one step further out. Letting Go write the verdict itself would
mean reimplementing:

  * the conditional `moderation=PENDING` guard in `_approve` / `_quarantine`,
    which is what stops a late classification overturning a moderator;
  * the `get_or_create` in `record_ai_report`, which is what stops a retry
    filing a second queue item;
  * the thresholds in `policy.combine_frames`, which decide when one weak
    frame is enough to hold a Swift back.

Every one of those is a *safety* rule with its own tests. A second copy in a
second language is a second set of the bugs they were written to prevent.

Not routed under `/api/swifts/` on purpose. These are machine endpoints with a
shared-secret gate and no user session; putting them under `/api/internal/`
keeps them out of the surface a phone can reach and makes them obvious to
anyone reading `core/urls.py`.
"""
from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from moderation import cloudflare_ai, policy
from moderation import text as text_filter

from . import ai_moderation
from .models import SwiftModerationJob


class WorkerAuthenticated(permissions.BasePermission):
    """A shared secret, not a user.

    `MODERATION_WORKER_TOKEN` is the same value the Go worker sends. Empty
    means these endpoints are closed entirely rather than open — the opposite
    of the usual "unset disables the check" default, because the failure of
    getting that backwards is that anything on the network can forge a
    moderation verdict.
    """

    def has_permission(self, request, view):
        expected = getattr(settings, 'MODERATION_WORKER_TOKEN', '')
        if not expected:
            return False
        header = request.META.get('HTTP_AUTHORIZATION', '')
        if not header.startswith('Bearer '):
            return False
        # `compare_digest` rather than `==`: comparing secrets with `==` leaks
        # their length and, in principle, their content through timing.
        from hmac import compare_digest

        return compare_digest(header[len('Bearer '):], expected)


class ModerationLeaseView(APIView):
    """GET /api/internal/moderation/lease/?limit=N

    Claims up to N due jobs and describes the work each one needs.

    The claim is `ai_moderation.claim` — the same atomic
    `UPDATE ... WHERE state='queued'` the in-process thread and the
    `moderate_swifts` sweeper already race through. So the Go worker is a third
    consumer of a protocol that already existed, and a job still goes to
    exactly one of the three. Nothing about the concurrency design had to
    change to add it.
    """

    authentication_classes = []
    permission_classes = [WorkerAuthenticated]

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get('limit', 4)), 1), 20)
        except (TypeError, ValueError):
            limit = 4

        leases = []
        for job_id in ai_moderation.due_jobs(limit=limit * 2):
            if len(leases) >= limit:
                break
            # A stale RUNNING row has to be put back before `claim` will take
            # it — the same two-step `sweep` does.
            SwiftModerationJob.objects.filter(
                pk=job_id, state=SwiftModerationJob.State.RUNNING,
            ).update(state=SwiftModerationJob.State.QUEUED)

            job = ai_moderation.claim(job_id)
            if job is None:
                continue  # somebody else got it; not an error
            lease = self._describe(job)
            if lease is None:
                # Nothing to sample. Hand it straight to the failure path
                # rather than leasing work that cannot be done.
                ai_moderation._fail(job, 'no media to sample', retryable=False)
                continue
            leases.append(lease)

        return Response({'leases': leases})

    def _describe(self, job):
        """Everything the worker needs, and nothing it could decide with."""
        swift = job.swift
        try:
            source = ai_moderation._source_url(swift)
        except cloudflare_ai.CloudflareError:
            return None
        if not source:
            return None

        duration = ai_moderation._effective_duration_ms(swift)
        return {
            'job_id': job.pk,
            'swift_id': swift.pk,
            'source_url': source,
            # The sampling plan is computed here, not there: "which frames
            # represent this video" is a policy question and it is already
            # tested in `moderation/test_policy.py`.
            'timestamps_ms': list(ai_moderation._frame_timestamps(swift, duration)),
            'duration_ms': duration,
            'want_audio': bool(getattr(settings, 'SWIFT_AI_AUDIO_MODERATION_ENABLED', False)),
            'frame_width': int(getattr(settings, 'SWIFT_AI_FRAME_WIDTH', 640)),
            # Sent down rather than duplicated in the Go repo. A filter prompt
            # is a specification for getting past the filter; it should have
            # one home.
            'prompt': cloudflare_ai.VISION_PROMPT,
            'vision_model': cloudflare_ai.vision_model(),
            'transcribe_model': cloudflare_ai.transcribe_model(),
        }


class ModerationSettleView(APIView):
    """POST /api/internal/moderation/settle/

    Takes raw per-frame model output and applies the existing decision policy.

    Note what the worker is *not* allowed to send: a verdict. There is no
    `approved` field in the payload, so there is no way for a compromised or
    buggy worker to publish a Swift — the worst it can do is report frames that
    were never classified, and `policy.combine_frames` treats a barely-sampled
    video as a failure rather than an approval.
    """

    authentication_classes = []
    permission_classes = [WorkerAuthenticated]

    def post(self, request):
        data = request.data
        job_id = data.get('job_id')
        job = (
            SwiftModerationJob.objects
            .select_related('swift__author')
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            return Response({'detail': 'unknown job'}, status=status.HTTP_404_NOT_FOUND)
        if job.state != SwiftModerationJob.State.RUNNING:
            # Already settled, or re-queued as stale and picked up elsewhere.
            # Not an error — a late arrival from a worker whose job was
            # reclaimed is exactly the race the stale sweep creates, and the
            # right answer is to ignore it rather than to apply it twice.
            return Response({'applied': False, 'reason': 'job is not running'})

        if data.get('error'):
            ai_moderation._fail(
                job, str(data['error'])[:300],
                retryable=bool(data.get('retryable', True)),
            )
            return Response({'applied': True, 'outcome': 'failed'})

        # Parse the raw answers with the parser that already has tests for
        # every way a model gets this wrong — prose around the JSON, a
        # confidence of "high", a 0-100 scale, invented categories.
        results, requested = [], 0
        for frame in data.get('frames') or []:
            requested += 1
            raw = frame.get('raw') or ''
            if not raw:
                continue  # fetch or model failure; counted, not classified
            try:
                results.append(cloudflare_ai.parse_vision_response(
                    raw, at_ms=frame.get('at_ms')))
            except cloudflare_ai.CloudflareError:
                # Unreadable output is a frame that was not classified, which
                # is what stops a broken model silently approving everything.
                continue

        decision = policy.combine_frames(
            results,
            requested=requested,
            threshold=float(getattr(settings, 'SWIFT_AI_UNSAFE_CONFIDENCE', 0.75)),
        )

        transcript = (data.get('transcript') or '').strip()
        if decision.approved and transcript:
            decision = policy.apply_text_verdict(
                decision, text_filter.classify(transcript), basis=policy.TRANSCRIPT)

        if decision.outcome == policy.FAILED:
            ai_moderation._fail(
                job, decision.error or 'classification incomplete', retryable=True)
            return Response({'applied': True, 'outcome': 'failed'})

        ai_moderation.settle(
            job, decision,
            audio_checked=bool(data.get('audio_checked')),
            model_used=data.get('model_used') or None,
        )
        return Response({
            'applied': True,
            'outcome': decision.outcome,
            'settled_at': timezone.now().isoformat(),
        })
