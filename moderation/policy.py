"""How a pile of per-frame results becomes one decision about one Swift.

Separated from both the Cloudflare client and the Django models on purpose:
every rule in here is a judgement call about how wrong a classifier is allowed
to be, and those are the rules most likely to be argued about and changed. They
are pure functions over dataclasses, so arguing about them costs a unit test
rather than a fixture, a network mock and a migration.

The shape of the argument
-------------------------
A frame classifier is wrong in two directions and the two cost wildly different
things. A false *negative* publishes something that should not have been
published — for a few minutes, until somebody reports it. A false *positive*
tells a person who did nothing wrong that their video broke the rules.

So there are three outcomes, not two:

    APPROVED     nothing objected, and enough of the video was actually looked
                 at for that to mean something.
    QUARANTINED  something objected. Held back, a human decides.
    FAILED       not enough of the video was successfully looked at to say
                 either. Held back too, but as "we do not know yet" rather than
                 "we think this is bad" — a different thing to tell the author,
                 and a job worth retrying rather than closing.

There is deliberately no fourth outcome that removes anything. Nothing in this
module ever returns `Moderation.REMOVED`, and nothing in it ever touches a user
account. A classifier that could ban people would be a classifier whose false
positive rate is somebody's account, and no confidence threshold makes that a
trade worth taking. Removal is a human at the admin, and only that.
"""
from dataclasses import dataclass, field

from .cloudflare_ai import REASON_SEVERITY, ModerationResult
from .reasons import Reason

APPROVED = 'approved'
QUARANTINED = 'quarantined'
FAILED = 'failed'

#: Why a Swift was held back, for the report note and the admin.
UNSAFE_CONFIDENT = 'unsafe_confident'
UNSAFE_UNCERTAIN = 'unsafe_uncertain'
UNSAFE_CORROBORATED = 'unsafe_corroborated'
CAPTION = 'caption'
TRANSCRIPT = 'transcript'
INSUFFICIENT_FRAMES = 'insufficient_frames'

#: Above this, one frame is enough on its own. Below it, one frame is a
#: suspicion that needs either a second frame agreeing or a human.
#:
#: 0.75 rather than 0.5 because the action taken at this threshold is still
#: only "hide it and ask a person", and the number that matters more is the one
#: below: an *uncertain* objection also hides the Swift. The threshold does not
#: decide whether the author is inconvenienced — it decides whether the queue
#: item says "this looks clearly wrong" or "have a look at this".
DEFAULT_UNSAFE_CONFIDENCE = 0.75

#: How much of the video has to have been classified for an all-clear to count.
#: Half the frames, and never fewer than two. One successful frame agreeing
#: that a video is safe is not a review of the video; it is a review of one
#: frame, and a Swift whose first frame is a title card would pass every time.
MIN_FRAME_FRACTION = 0.5
MIN_FRAMES = 2


@dataclass(frozen=True)
class Decision:
    """The outcome, and everything the audit trail needs to explain it.

    `reason` is a `moderation.reasons.Reason` so the report this produces sorts
    into the same buckets a human report does — the whole argument of
    `moderation/reasons.py`, applied to a machine reporter.
    """

    outcome: str = APPROVED
    reason: str = ''
    #: One of the module constants above. Machine-readable, for the admin
    #: column and the tests; never shown to the author.
    basis: str = ''
    confidence: float = 0.0
    categories: tuple = field(default_factory=tuple)
    #: Timestamps actually classified, in ms. The evidence that the video was
    #: sampled across its length rather than at one point.
    sampled_ms: tuple = field(default_factory=tuple)
    frames_requested: int = 0
    frames_classified: int = 0
    #: Set when the pipeline could not complete. Never shown to the author and
    #: never returned by the API — it is provider detail.
    error: str = ''

    @property
    def approved(self):
        return self.outcome == APPROVED

    @property
    def publishable(self):
        """Only an approval publishes. Both other outcomes hold the Swift."""
        return self.outcome == APPROVED


# ─── Sampling ─────────────────────────────────────────────────────────────────
def frame_count_for(duration_ms, *, configured=None, maximum=12):
    """How many frames to look at for a video this long.

    Bounded at both ends and by duration, which is the point: a per-second or
    per-keyframe strategy makes the cost of moderating a Swift a function of
    how long somebody made it, and the ceiling on a Swift is sixty seconds, so
    the difference between the cheapest and most expensive video would be
    sixtyfold for no gain in what is actually caught.

    A short clip does not need eight frames — there is not eight frames' worth
    of distinct content in four seconds — and a sixty-second one is not usefully
    served by three.
    """
    if configured:
        return max(1, min(int(configured), maximum))
    seconds = max(duration_ms or 0, 0) / 1000
    if seconds <= 10:
        count = 5
    elif seconds <= 30:
        count = 8
    else:
        count = 10
    return max(1, min(count, maximum))


def sample_timestamps(duration_ms, count):
    """`count` timestamps in ms, spread across the video, first and last included.

    Spread from 5% to 95% rather than 0% to 100%. The two ends are where the
    useless frames live: a video's first frame is often black or a hard cut,
    and its last is often the same, so anchoring exactly at the extremes spends
    two of a small budget on frames that tell a classifier nothing. Five per
    cent in from each end still samples "the beginning" and "the end" in the
    sense requirement 3 means — the parts a person trying to sneak something
    past a mid-video sampler would use.

    Deduplicated, because the timestamps are rendered to one decimal place and
    a very short clip can produce collisions.
    """
    duration = max(duration_ms or 0, 0)
    if count <= 1 or duration <= 0:
        return (0,) if duration <= 0 else (int(duration * 0.5),)
    first, last = 0.05, 0.95
    step = (last - first) / (count - 1)
    seen = []
    for i in range(count):
        # Rounded to 100ms because that is the resolution `_format_time`
        # renders at; carrying more precision here would produce distinct
        # numbers that become identical URLs.
        at = int(round(duration * (first + i * step) / 100.0) * 100)
        if at not in seen:
            seen.append(at)
    return tuple(seen)


# ─── Combining ────────────────────────────────────────────────────────────────
def _worst_reason(reasons):
    for candidate in REASON_SEVERITY:
        if candidate in reasons:
            return candidate
    return Reason.OTHER


def combine_frames(results, *, requested, threshold=DEFAULT_UNSAFE_CONFIDENCE):
    """One `Decision` from the frame results that came back.

    `requested` is how many frames were *asked for*, not how many arrived, and
    the difference is the whole reason it is a separate argument: a run where
    eight frames were requested and one came back is not a video that passed —
    it is a video that was barely looked at, and calling that an approval is
    how a fail-open bug gets written.

    The three-way rule on unsafe frames:

      one frame, confident         -> quarantine, and say so. A single frame is
                                      enough *to ask a human*, which is all
                                      this outcome does.
      two or more frames, any      -> quarantine. Independent agreement across
                                      the video is stronger evidence than any
                                      one frame's self-reported confidence.
      one frame, not confident     -> quarantine as uncertain. Same visibility
                                      outcome, different queue note, and the
                                      distinction is what stops a moderator
                                      reading every item as equally damning.
    """
    classified = [r for r in results if isinstance(r, ModerationResult)]
    sampled = tuple(r.at_ms for r in classified if r.at_ms is not None)
    unsafe = [r for r in classified if not r.safe]

    if unsafe:
        strongest = max(unsafe, key=lambda r: r.confidence)
        reasons = {reason for r in unsafe for reason in r.reasons}
        categories = tuple(dict.fromkeys(c for r in unsafe for c in r.categories))
        if strongest.confidence >= threshold:
            basis = UNSAFE_CONFIDENT
        elif len(unsafe) > 1:
            basis = UNSAFE_CORROBORATED
        else:
            basis = UNSAFE_UNCERTAIN
        return Decision(
            outcome=QUARANTINED,
            reason=_worst_reason(reasons),
            basis=basis,
            confidence=strongest.confidence,
            categories=categories,
            sampled_ms=sampled,
            frames_requested=requested,
            frames_classified=len(classified),
        )

    # Nothing came back at all. Guarded before the proportional rule below,
    # because that rule compares against `min(needed, requested)` — and with
    # `requested` zero the comparison is `0 < 0`, which is False, which would
    # make "no frames were classified" an *approval*.
    #
    # That was unreachable while the only caller passed `requested=len(
    # timestamps)`, which is never zero. Exposing the decision over HTTP to the
    # Go worker made it reachable: an empty frame list would have published the
    # Swift. Found by
    # `swifts/test_worker_endpoints.py::test_a_worker_cannot_publish_a_swift_by_asserting_it_is_safe`.
    #
    # The rule stated positively: an approval requires evidence, and zero
    # frames is not evidence.
    if not classified:
        return Decision(
            outcome=FAILED,
            basis=INSUFFICIENT_FRAMES,
            frames_requested=requested,
            frames_classified=0,
            error='no frames were classified',
        )

    needed = max(MIN_FRAMES, int(requested * MIN_FRAME_FRACTION + 0.999))
    if len(classified) < min(needed, requested):
        return Decision(
            outcome=FAILED,
            basis=INSUFFICIENT_FRAMES,
            sampled_ms=sampled,
            frames_requested=requested,
            frames_classified=len(classified),
            error=f'only {len(classified)} of {requested} frames were classified',
        )

    return Decision(
        outcome=APPROVED,
        sampled_ms=sampled,
        frames_requested=requested,
        frames_classified=len(classified),
    )


def apply_text_verdict(decision, verdict, *, basis):
    """Fold a `moderation.text.Verdict` into a video decision.

    Text and video are separate judgements and either one can hold a Swift
    back, which is requirement 10 stated as code: a safe video with a
    prohibited caption is not publishable, and neither is a prohibited video
    with a blameless caption.

    An existing quarantine is never downgraded, and a `FAILED` run is never
    upgraded to a quarantine by a text verdict — the video still has not been
    looked at, and closing the job on the strength of the caption would leave
    the frames permanently unclassified.
    """
    if verdict is None or verdict.allowed:
        return decision
    if decision.outcome == QUARANTINED:
        return decision
    if decision.outcome == FAILED:
        return decision
    return Decision(
        outcome=QUARANTINED,
        reason=verdict.reason or Reason.OTHER,
        basis=basis,
        confidence=1.0 if verdict.blocked else 0.5,
        categories=decision.categories,
        sampled_ms=decision.sampled_ms,
        frames_requested=decision.frames_requested,
        frames_classified=decision.frames_classified,
    )
