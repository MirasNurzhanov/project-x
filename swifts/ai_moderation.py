"""Screening a Swift's media before anybody can watch it.

Where this sits
---------------
`moderation.cloudflare_ai` knows how to talk to Cloudflare and nothing else.
`moderation.policy` knows how a pile of frame results becomes one decision and
nothing else. This module is the only place the two meet a database row: it
reads a `SwiftVideo`, drives the pipeline, and writes the outcome to the three
places that have to agree about it — the Swift's `moderation` column, the job
row, and the moderation queue.

The lifecycle
-------------
    POST /api/swifts/     the row is saved with moderation=PENDING and a
                          SwiftModerationJob alongside it, in one transaction.
                          PENDING is in `moderation.reasons.HIDDEN_STATES`, so
                          from the instant it exists the Swift is invisible to
                          everybody except its author. The response is sent.

    on_commit             a daemon thread picks the job up. Latency only —
                          losing it loses nothing, because the job row is still
                          queued and `manage.py moderate_swifts` will find it.

    run()                 frames out of Media Transformations, each one through
                          the vision model, the caption through the existing
                          text filter, optionally the audio through
                          transcription and then the *same* text filter.

    settle()              APPROVED -> moderation=OK, and only then is it in a
                          feed. QUARANTINED -> stays hidden, and a SwiftReport
                          with source=AI_VIDEO puts it in front of a human.
                          FAILED -> stays PENDING and the job retries.

Three properties worth stating outright, because each one is a bug that would
be invisible if it were wrong:

  * **Nothing here publishes by default.** Every path that does not reach an
    explicit approval leaves the Swift hidden. Cloudflare being down, the token
    being wrong, the model returning gibberish, the process being killed
    mid-run — all of them end with a Swift nobody can watch, which is the only
    acceptable direction for this to fail in.

  * **A late "safe" cannot resurrect a moderator's decision.** Every write of
    the `moderation` column is a conditional UPDATE filtered on the state the
    job expects to find. If a human removed the Swift while the classifier was
    running, the classifier's approval matches zero rows and changes nothing.

  * **A retry files no second report.** The job row holds a reference to the
    one it filed, and `get_or_create` on (swift, reporter) is the constraint
    underneath that. Two runs of the same job produce one queue item.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from moderation import cloudflare_ai, policy
from moderation import text as text_filter
from moderation.models import ModerationAction
from moderation.reasons import Action, Moderation

from .models import SwiftModerationJob, SwiftReport, SwiftVideo

logger = logging.getLogger(__name__)


def _setting(name, default=None):
    return getattr(settings, name, default)


def enabled():
    """Whether new Swifts should be held for AI moderation at all.

    Off by default. With it off this module never runs and a Swift is published
    exactly as it was before the feature existed — which is what makes the
    change shippable ahead of the Cloudflare dashboard work, and what makes
    turning it on a variable change rather than a release.
    """
    return bool(_setting('SWIFT_AI_MODERATION_ENABLED', False))


# ─── Starting a job ───────────────────────────────────────────────────────────
def initial_moderation(verdict):
    """The `moderation` value a freshly posted Swift is saved with.

    Text first, and that is deliberate rather than incidental: a caption the
    filter flagged is quarantined *now*, on a rule that has already been
    decided, and does not spend a frame of AI budget to reach the same
    conclusion. `moderation.services.moderation_for` is the existing answer for
    everything that is not a video, and this is that answer with one clause in
    front of it.
    """
    from moderation import services as moderation_services

    if verdict is not None and verdict.flagged:
        return Moderation.QUARANTINED
    if enabled():
        return Moderation.PENDING
    return moderation_services.moderation_for(verdict)


def enqueue(swift):
    """Create this Swift's job and start work on it. Returns the job, or None.

    Called inside the create view's request. The *creation* is synchronous —
    one INSERT, so the durable record exists before the response goes out — and
    the *work* is not, which is the split requirement 11 asks for: the phone
    gets its 201 as soon as the upload lands, and finds out the verdict by
    polling the Swift it was handed.

    A Swift with no uploaded file gets no job. That is every legacy
    Cloudinary-era row and every fixture in the test suite, and neither has
    anything at a URL for Cloudflare to fetch.
    """
    if not enabled():
        return None
    if swift.moderation != Moderation.PENDING:
        return None
    if not swift.video:
        # Nothing to sample. Fail closed rather than approving an unscreenable
        # row: it stays PENDING and a human can release it from the admin.
        logger.warning('[SwiftAI] swift %s is PENDING with no video file', swift.pk)
        return None

    job, _ = SwiftModerationJob.objects.get_or_create(swift=swift)
    if _setting('SWIFT_AI_MODERATION_SYNC', False):
        # Tests, where `on_commit` never fires: `TestCase` wraps each case in a
        # transaction it rolls back, so a callback registered for commit is
        # simply dropped. Running inline here is the same escape hatch
        # `notifications.push` uses, and it keeps the assertion in a test about
        # a finished state rather than about a callback list.
        start(job.pk)
    else:
        # After commit, never before: a worker that started while the row was
        # still uncommitted would look for a Swift no other connection can see.
        transaction.on_commit(lambda: start(job.pk))
    return job


def start(job_id):
    """Run job `job_id`, off the request thread.

    The same shape `notifications.push._queue` uses, for the same reason and
    with the same escape hatch: under the test settings the thread is not
    started and the work runs inline, so a test asserts on a finished state
    rather than on a sleep, and no daemon thread outlives the case that spawned
    it.

    Unlike a push, losing this thread is not losing the work — the job row is
    still `QUEUED` and the sweeper will claim it. That is the only reason a
    thread is an acceptable executor here.
    """
    if _setting('SWIFT_AI_MODERATION_SYNC', False):
        run(job_id)
        return
    threading.Thread(target=run, args=(job_id,), daemon=True).start()


# ─── Running one job ──────────────────────────────────────────────────────────
def claim(job_id):
    """Take ownership of a job, or return None if somebody else has it.

    A conditional UPDATE, not a read-then-write. The sweeper and the in-process
    thread genuinely race — the thread is started on commit and the sweeper
    runs on a timer, and a slow commit puts them microseconds apart — so the
    only safe claim is one the database arbitrates. Two winners would mean two
    classifications billed and two reports filed.
    """
    now = timezone.now()
    claimed = SwiftModerationJob.objects.filter(
        pk=job_id, state__in=(SwiftModerationJob.State.QUEUED,),
    ).update(
        state=SwiftModerationJob.State.RUNNING,
        attempts=F('attempts') + 1,
        next_attempt_at=None,
        updated_at=now,
    )
    if not claimed:
        return None
    # Re-read rather than use the in-memory row: `attempts` is an `F`
    # expression until it comes back from the database, and `_fail` compares it
    # to a number.
    return SwiftModerationJob.objects.select_related('swift__author').filter(
        pk=job_id).first()


def run(job_id):
    """Classify one Swift end to end. Never raises.

    Never raises because both of its callers are places an exception would be
    lost: a daemon thread, where it would print a traceback nobody reads, and a
    management command sweeping a batch, where it would abandon every job after
    this one. Everything that can go wrong is a `FAILED` decision instead, and
    a `FAILED` decision leaves the Swift hidden.
    """
    job = claim(job_id)
    if job is None:
        return None

    swift = job.swift
    try:
        decision, transcript_checked = _classify(swift)
    except cloudflare_ai.CloudflareError as exc:
        # Configuration errors and credential errors land here too, and they
        # are the ones the `retryable` flag matters for: retrying a 403 for a
        # token without Workers AI permission burns quota to be told the same
        # thing three times.
        return _fail(job, str(exc), retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.exception('[SwiftAI] job %s crashed', job_id)
        return _fail(job, f'{type(exc).__name__}: {exc}', retryable=False)

    if decision.outcome == policy.FAILED:
        return _fail(job, decision.error or 'classification incomplete', retryable=True)

    return settle(job, decision, audio_checked=transcript_checked)


def _classify(swift):
    """`(Decision, audio_checked)` for one Swift. Talks to Cloudflare.

    The order is chosen so the cheapest thing that can hold a Swift back runs
    first. The caption verdict is a regex over a few hundred characters; a
    frame classification is a paid network round trip. Screening the caption
    afterwards would spend the whole frame budget on a Swift already destined
    for the queue.
    """
    caption_verdict = text_filter.classify_many(swift.caption, swift.overlay_text)
    if caption_verdict.blocked or caption_verdict.flagged:
        decision = policy.Decision(outcome=policy.APPROVED)
        return policy.apply_text_verdict(
            decision, caption_verdict, basis=policy.CAPTION), False

    source = _source_url(swift)
    duration = _effective_duration_ms(swift)
    timestamps = _frame_timestamps(swift, duration)

    results = []
    failures = []
    for at_ms in timestamps:
        try:
            frame = cloudflare_ai.fetch_frame(source, at_ms)
            results.append(cloudflare_ai.classify_image(frame, at_ms=at_ms))
        except cloudflare_ai.MissingCredentials:
            # Not a per-frame problem. Abandon the run so the job records a
            # configuration failure rather than "1 of 8 frames classified".
            raise
        except cloudflare_ai.CloudflareError as exc:
            # One bad frame is survivable — a timestamp past the end of a
            # mis-reported duration, a single 5xx. `combine_frames` is what
            # decides whether enough of them survived to mean anything, and it
            # refuses to call a barely-sampled video approved.
            #
            # `redact` rather than the exception verbatim: this string came
            # from a provider, and the defence against a credential reaching a
            # log belongs at the point the string is written as well as the
            # point it is built.
            failures.append(exc)
            logger.warning(
                '[SwiftAI] swift %s frame %sms: %s',
                swift.pk, at_ms, cloudflare_ai.redact(exc))

    if not results and failures and not any(e.retryable for e in failures):
        # Every frame failed and none of the failures will ever answer
        # differently — transformations not enabled on the zone, a token
        # without Workers AI permission, a source origin that is not allowed.
        # Re-raising here is what stops three full passes of the frame budget
        # being billed to be told the same thing three times; `run` turns it
        # into a job that has given up and a Swift a human can release.
        raise failures[-1]

    decision = policy.combine_frames(
        results,
        requested=len(timestamps),
        threshold=float(_setting('SWIFT_AI_UNSAFE_CONFIDENCE', 0.75)),
    )

    audio_checked = False
    if decision.approved and _setting('SWIFT_AI_AUDIO_MODERATION_ENABLED', False):
        decision, audio_checked = _screen_audio(swift, source, duration, decision)

    return decision, audio_checked


def _screen_audio(swift, source, duration_ms, decision):
    """Fold what is *said* in the Swift into the decision. Best effort.

    Deliberately non-fatal: a video that has been classified frame by frame and
    found clean is not held back because the audio track could not be fetched.
    The transcript is a second opinion on a different channel, not a second
    gate, and treating a transcription failure as a moderation failure would
    make the optional half of this feature able to break the mandatory half.

    The transcript goes through `moderation.text`, the same classifier the
    caption went through. That is the whole point of transcribing at all: a
    spoken slur and a typed one are the same rule, and a second filter for the
    spoken case is a second filter to keep in step.
    """
    try:
        audio = cloudflare_ai.fetch_audio(source, duration_s=duration_ms / 1000)
        transcript = cloudflare_ai.transcribe(audio)
    except cloudflare_ai.CloudflareError as exc:
        logger.warning(
            '[SwiftAI] swift %s audio: %s', swift.pk, cloudflare_ai.redact(exc))
        return decision, False

    if not transcript.text:
        return decision, True
    verdict = text_filter.classify(transcript.text)
    return policy.apply_text_verdict(
        decision, verdict, basis=policy.TRANSCRIPT), True


def _source_url(swift):
    """The absolute URL Cloudflare should fetch the video from.

    `swifts.media.playback_url` rather than a second way of resolving the same
    thing — the classifier has to look at the file a viewer would be served, or
    it is moderating something else.
    """
    from . import media

    url = media.playback_url(swift)
    if url and url.startswith('//'):
        url = f'https:{url}'
    if url and not url.startswith('http'):
        raise cloudflare_ai.CloudflareError(
            'Swift media is not at an absolute URL Cloudflare can fetch.',
            retryable=False,
        )
    return url


def _frame_timestamps(swift, duration_ms):
    """Source timestamps covering the trim the viewer actually watches."""
    count = policy.frame_count_for(
        duration_ms,
        configured=_setting('SWIFT_AI_FRAME_COUNT', 0),
        maximum=int(_setting('SWIFT_AI_FRAME_MAX', 12)),
    )
    # Playback seeks within the original file; Cloudflare receives that same
    # untrimmed file, so a position within the clip needs the source offset.
    start = swift.trim_start_ms or 0
    return tuple(start + at_ms for at_ms in policy.sample_timestamps(duration_ms, count))


def _effective_duration_ms(swift):
    """How long the part of the video anybody actually watches is.

    The trim, not the source. Since the R2 migration the file on disk is the
    untrimmed recording and the player applies the range at playback (see
    `swifts.media`), so sampling across the *source* duration would spend
    frames on footage no viewer is ever shown — and, worse, would under-sample
    the part they are.
    """
    from .media import MAX_DURATION_MS

    start = swift.trim_start_ms or 0
    end = swift.trim_end_ms
    if end and end > start:
        return min(end - start, MAX_DURATION_MS)
    if swift.duration_ms:
        return min(max(swift.duration_ms - start, 0), MAX_DURATION_MS)
    return MAX_DURATION_MS


# ─── Recording the outcome ────────────────────────────────────────────────────
@transaction.atomic
def settle(job, decision, *, audio_checked=False, model_used=None):
    """Write a finished decision to the Swift, the job and the queue.

    One transaction, because the three have to agree. A Swift quarantined with
    no queue item is content that has disappeared and that nobody is going to
    look at; a queue item pointing at an approved Swift is a moderator's time
    spent on nothing.
    """
    retrying_failure = (
        job.outcome == policy.FAILED
        and job.basis == policy.INSUFFICIENT_FRAMES
        and job.report_id is not None
    )
    job.outcome = decision.outcome
    job.basis = decision.basis
    job.categories = ','.join(decision.categories)[:200]
    job.confidence = decision.confidence
    job.sampled_ms = ','.join(str(ms) for ms in decision.sampled_ms)[:200]
    job.frames_requested = decision.frames_requested
    job.frames_classified = decision.frames_classified
    # The model that actually ran. Defaults to this process's configured one,
    # which is right when the in-process path did the work — but the Go worker
    # in services/go-moderation is told which model to call in its lease, and
    # that value can differ from what this process would have picked if the
    # setting changed mid-flight. Recording what ran beats recording what we
    # would have run.
    job.model_used = (model_used or cloudflare_ai.vision_model())[:120]
    job.audio_checked = audio_checked
    job.state = SwiftModerationJob.State.DONE
    job.finished_at = timezone.now()
    job.last_error = ''

    if decision.approved:
        approved = _approve(job.swift)
        if approved and retrying_failure:
            _unreviewed_ai_report(job).update(
                state=SwiftReport.State.REVIEWED,
                reviewed_at=timezone.now(),
                moderator_note='AI moderation approved on retry.',
            )
    else:
        job.report = _quarantine(job.swift, decision)
        if retrying_failure:
            _unreviewed_ai_report(job).update(
                reason=decision.reason or SwiftReport.Reason.OTHER,
                note=_report_note(decision)[:500],
            )

    job.save(update_fields=[
        'outcome', 'basis', 'categories', 'confidence', 'sampled_ms',
        'frames_requested', 'frames_classified', 'model_used', 'audio_checked',
        'state', 'finished_at', 'last_error', 'report', 'updated_at',
    ])
    return job


def _approve(swift):
    """PENDING -> OK. Nothing else, ever.

    The `moderation=PENDING` clause is the guard requirement 15 asks for by
    name: a moderator who removed this Swift while the classifier was still
    running has made a decision the classifier does not get to overturn by
    finishing afterwards. The UPDATE matches zero rows in that case and the
    Swift stays removed, which is the correct outcome of a race nobody can
    prevent.
    """
    return SwiftVideo.objects.filter(
        pk=swift.pk, moderation=Moderation.PENDING,
    ).update(moderation=Moderation.OK)


def _quarantine(swift, decision):
    """Hold the Swift back and put it in front of a human. Returns the report.

    Only from PENDING, for the same reason `_approve` only writes from PENDING:
    a Swift a moderator has already REMOVED must not be quietly promoted back
    to a state that a restore action would then treat as reviewable, and one
    they have already restored to OK must not be re-hidden by a job finishing
    late.

    The report is filed whether or not the state change landed. A job that
    reached a quarantine decision produces a queue item even in the race above,
    because the evidence is worth having in front of the person who made the
    other decision.
    """
    SwiftVideo.objects.filter(
        pk=swift.pk, moderation=Moderation.PENDING,
    ).update(moderation=Moderation.QUARANTINED)

    report = record_ai_report(swift, decision)
    ModerationAction.objects.create(
        kind=ModerationAction.Kind.CONTENT_REMOVED,
        subject=swift.author,
        # No moderator: this was a rule, not a person. `ModerationAction`
        # documents that null moderator means exactly this.
        detail=(
            f'Swift #{swift.pk} quarantined by AI video moderation '
            f'({decision.basis}, confidence {decision.confidence:.2f})'
        )[:300],
    )
    return report


def record_ai_report(swift, decision):
    """File the AI's objection as an ordinary `SwiftReport`. Idempotent.

    Ordinary is the point. There is no second dashboard, no separate AI queue
    and no new admin screen: the item lands in `SwiftReportAdmin` next to the
    ones people filed, sorted by the same rules, actioned by the same four
    buttons, and closed by the same `moderation.services.resolve_report`. The
    only thing that distinguishes it is `source=AI_VIDEO`, which is there so a
    moderator can tell what raised it — not so it can be routed somewhere else.

    `get_or_create` on the unique (swift, reporter) pair is what makes a retry
    safe. The same job running twice finds its own row the second time.
    """
    report, _ = SwiftReport.objects.get_or_create(
        swift=swift,
        reporter=swift.author,
        defaults={
            'reason': decision.reason or SwiftReport.Reason.OTHER,
            'source': SwiftReport.Source.AI_VIDEO,
            # Category names and a rounded confidence — never the prompt, never
            # the model's prose, never a frame. A note is read in a list view
            # by a person deciding what to open first.
            'note': _report_note(decision)[:500],
        },
    )
    return report


def _report_note(decision):
    parts = [f'AI video moderation: {decision.basis or "flagged"}']
    if decision.categories:
        parts.append(f'categories {", ".join(decision.categories)}')
    parts.append(f'confidence {decision.confidence:.2f}')
    if decision.sampled_ms:
        parts.append(
            f'{decision.frames_classified}/{decision.frames_requested} frames at '
            + ', '.join(f'{ms}ms' for ms in decision.sampled_ms[:8])
        )
    return '; '.join(parts)


# ─── Failing ──────────────────────────────────────────────────────────────────
def _fail(job, message, *, retryable):
    """Record a failed attempt and decide whether to try again.

    The Swift is untouched: it was PENDING when the job started and it is
    PENDING now, which means it is not in anybody's feed. That is the fail-safe
    behaviour requirement 14 asks for, and it costs nothing to implement
    because it is the *absence* of an action rather than a compensating one.
    """
    max_attempts = int(_setting('SWIFT_AI_MAX_ATTEMPTS', 3))
    job.last_error = str(message)[:300]

    if not retryable or job.attempts >= max_attempts:
        return exhaust(job)

    job.state = SwiftModerationJob.State.QUEUED
    # Exponential, from a minute. Long enough that a Cloudflare incident is not
    # made worse by this, short enough that a creator is not waiting an hour on
    # a blip.
    job.next_attempt_at = timezone.now() + timedelta(
        seconds=60 * (2 ** max(job.attempts - 1, 0)))
    job.save(update_fields=['state', 'next_attempt_at', 'last_error', 'updated_at'])
    return job


@transaction.atomic
def exhaust(job):
    """Out of attempts. Quarantine for a human rather than leave it pending.

    The alternative — leave it PENDING forever — is safe for viewers and
    terrible for everybody else: the author is never told anything, and the
    Swift is in no queue, so nobody ever looks at it. Quarantining converts a
    silent limbo into a visible backlog item with an author who has been given
    a neutral explanation.

    Note what this is *not*: it is not a finding that the content is bad. The
    report says the pipeline could not complete, and the moderator's ordinary
    "restore" action publishes it.
    """
    decision = policy.Decision(
        outcome=policy.QUARANTINED,
        reason=SwiftReport.Reason.OTHER,
        basis=policy.INSUFFICIENT_FRAMES,
        confidence=0.0,
        frames_requested=job.frames_requested,
        frames_classified=job.frames_classified,
    )
    SwiftVideo.objects.filter(
        pk=job.swift_id, moderation=Moderation.PENDING,
    ).update(moderation=Moderation.QUARANTINED)

    job.report = record_ai_report(job.swift, decision)
    job.state = SwiftModerationJob.State.FAILED
    job.outcome = policy.FAILED
    job.basis = policy.INSUFFICIENT_FRAMES
    job.finished_at = timezone.now()
    job.next_attempt_at = None
    job.save(update_fields=[
        'state', 'outcome', 'basis', 'report', 'finished_at',
        'next_attempt_at', 'last_error', 'updated_at',
    ])
    return job


# ─── The sweeper ──────────────────────────────────────────────────────────────

def _unreviewed_ai_report(job):
    return SwiftReport.objects.filter(
        pk=job.report_id, swift_id=job.swift_id,
        source=SwiftReport.Source.AI_VIDEO,
        state=SwiftReport.State.OPEN,
        reviewed_at__isnull=True, reviewed_by__isnull=True,
        action_taken=Action.NONE, moderator_note='',
    )


@transaction.atomic
def retry_failed(swift_ids):
    """Requeue selected exhausted jobs without publishing or undoing review.

    Both records change in one transaction. Keep the failed outcome and basis
    until settlement so a successful retry can close its old failure report.
    Content rejected by the classifier has a DONE job and is never eligible.
    """
    requeued = []
    jobs = SwiftModerationJob.objects.select_for_update().filter(
        swift_id__in=set(swift_ids),
        state=SwiftModerationJob.State.FAILED,
        outcome=policy.FAILED,
        basis=policy.INSUFFICIENT_FRAMES,
    ).order_by('pk')
    for job in jobs:
        swift = SwiftVideo.objects.select_for_update().filter(
            pk=job.swift_id, moderation=Moderation.QUARANTINED,
            deleted_at__isnull=True,
        ).first()
        if swift is None or not swift.video:
            continue
        report = _unreviewed_ai_report(job).select_for_update().first()
        if report is None:
            continue
        # An independent report or a human decision still needs human review.
        if SwiftReport.objects.filter(swift_id=swift.pk).exclude(pk=report.pk).exists():
            continue
        changed = SwiftVideo.objects.filter(
            pk=swift.pk, moderation=Moderation.QUARANTINED,
            deleted_at__isnull=True,
        ).update(moderation=Moderation.PENDING)
        if not changed:
            continue
        job.state = SwiftModerationJob.State.QUEUED
        job.attempts = 0
        job.next_attempt_at = None
        job.finished_at = None
        job.last_error = ''
        job.save(update_fields=[
            'state', 'attempts', 'next_attempt_at', 'finished_at',
            'last_error', 'updated_at',
        ])
        requeued.append(job.pk)
    return requeued


def due_jobs(limit=50):
    """Jobs that should be running and are not.

    Two populations, and the second is the reason this exists at all:

      queued & due     a first attempt whose thread never ran (the process was
                       replaced between the commit and the thread starting), or
                       a retry whose backoff has elapsed.
      running & stale  a job whose worker died mid-flight. Without this they
                       would sit in RUNNING forever and their Swifts would stay
                       invisible with nothing scheduled to look at them again.
    """
    now = timezone.now()
    stale_after = int(_setting('SWIFT_AI_STALE_AFTER_S', 900))
    stale_before = now - timedelta(seconds=stale_after)

    return list(
        SwiftModerationJob.objects.filter(
            Q(state=SwiftModerationJob.State.QUEUED,
              next_attempt_at__isnull=True)
            | Q(state=SwiftModerationJob.State.QUEUED,
                next_attempt_at__lte=now)
            | Q(state=SwiftModerationJob.State.RUNNING,
                updated_at__lte=stale_before)
        )
        .order_by('created_at')
        .values_list('pk', flat=True)[:limit]
    )


def sweep(limit=50):
    """Run every due job. Returns how many were actually claimed.

    Claimed, not attempted: a job the in-process thread grabbed first is not a
    failure of the sweep, and counting it as one would make the number useless
    for spotting a real backlog.
    """
    done = 0
    for job_id in due_jobs(limit):
        # A stale RUNNING job has to be put back before `claim` will take it.
        SwiftModerationJob.objects.filter(
            pk=job_id, state=SwiftModerationJob.State.RUNNING,
        ).update(state=SwiftModerationJob.State.QUEUED)
        if run(job_id) is not None:
            done += 1
    return done
