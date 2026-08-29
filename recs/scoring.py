"""Turning features into a number.

## This is not a model, and does not pretend to be one

Approx has no trained ranker, because Approx has no training data yet — the table
that will hold it (`SwiftSignal`) was created in this same change and is empty.
So what follows is a **deterministic estimator**: each head is a hand-written
function of the features, and the heads are combined with the weights in
`params.py`. Every number it produces can be explained by pointing at a line.

That is a deliberate choice and not a placeholder apology. A logistic
regression fitted to no data is worse than an honest heuristic, and a neural
network claimed but not present is worse than both.

## The shape is the model's shape, on purpose

X's heavy ranker predicts a set of engagement probabilities — `PredictedFav`,
`PredictedReply`, `PredictedVideoWatchTime`, `PredictedNegativeFeedback` — and
`WeighedModelRerankingScorer` reduces them to one score with a configurable
weight per head. This file has exactly that structure:

    estimate_heads(features)  ->  {head: probability}
    aggregate(heads, weights) ->  float

Replacing the estimator with a model means replacing `estimate_heads` and
nothing else. The pipeline, the weights, the diversity pass, the API and the
tests all sit on the far side of that boundary and none of them change. That is
the property the brief asks for, and it is why the two functions are separate
rather than one loop.

## Light vs heavy

The same split X uses, for the same reason. `light_score` is a cheap function
of columns already on the candidate row — no joins, no per-viewer state — and
runs over the whole pool to cut it down. `heavy_score` runs over the survivors
with the full hydrated feature set. Light ranking exists so that hydration,
which is the expensive part, never runs over 400 candidates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .params import load_params

# ── Head names ────────────────────────────────────────────────────────────────
# Strings rather than an enum because they are also metric names, log keys and,
# eventually, model output names. One spelling for all four.
COMPLETION = 'completion'
WATCH_TIME = 'watch_time'
REPLAY = 'replay'
LIKE = 'like'
COMMENT = 'comment'
REPOST = 'repost'
PROFILE_OPEN = 'profile_open'
LOCATION_REVEAL = 'location_reveal'
SKIP = 'skip'
NEGATIVE_FEEDBACK = 'negative_feedback'
CREATOR_FATIGUE = 'creator_fatigue'
TOPIC_FATIGUE = 'topic_fatigue'
SEEN_BEFORE = 'seen_before'

HEADS = (
    COMPLETION, WATCH_TIME, REPLAY, LIKE, COMMENT, REPOST,
    PROFILE_OPEN, LOCATION_REVEAL, SKIP, NEGATIVE_FEEDBACK,
    CREATOR_FATIGUE, TOPIC_FATIGUE, SEEN_BEFORE,
)


def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _affinity_multiplier(affinity):
    """Affinity → a bounded multiplier around 1.0.

    `tanh` rather than a linear term for one specific reason: affinity is an
    unbounded running total, so a viewer who has watched one creator two
    hundred times would otherwise produce a multiplier large enough that no
    other feature could ever matter again. This saturates at ×2, so a strongly
    liked creator is favoured decisively but never absolutely.
    """
    return 1.0 + math.tanh(affinity / 25.0)


def _recency_decay(age_hours):
    """Fresh is better, with a long tail rather than a cliff.

    Half-life of two days. A cliff makes the feed lurch when a batch of content
    crosses it; a decay just reorders gently.
    """
    return math.pow(0.5, age_hours / 48.0)


# ── Light ranking ─────────────────────────────────────────────────────────────
def light_score(row, *, now, is_friend=False, source_agreement=1):
    """A cheap ordering over the whole candidate pool.

    Reads only columns already on the `SwiftVideo` row plus two facts the
    caller has for free. No queries, no per-candidate state. Its only job is to
    decide which candidates are worth hydrating — being roughly right is
    sufficient and being fast is not optional.
    """
    age_hours = max(0.0, (now - row.created_at).total_seconds() / 3600.0)
    engagement = row.like_count + row.comment_count * 2 + row.repost_count * 3
    reached = max(row.view_count, 1)

    score = (engagement / reached) * 10.0
    score += _recency_decay(age_hours) * 5.0
    score += math.log1p(source_agreement) * 2.0
    if is_friend:
        score += 3.0
    return score


# ── Heavy ranking ─────────────────────────────────────────────────────────────
def estimate_heads(f) -> dict:
    """Predicted engagement probabilities for one candidate.

    **This is the function a trained model replaces.** Its contract: take a
    `features.Features`, return a dict keyed by the names in `HEADS` with
    values in [0, 1] (or, for `watch_time`, a value normalised to a reference
    length so it stays on the same scale as the rest).

    Each estimate below is a population rate adjusted by what we know about
    this specific viewer. Where we know nothing, the rate falls back to the
    smoothed prior from `features.py`, which is the honest answer.
    """
    affinity = _affinity_multiplier(f.creator_affinity)
    interest = 1.0 + min(f.shared_interests, 5) * 0.08
    social = 1.15 if f.is_friend else 1.0

    # Age, applied to the *tap* heads rather than as a head of its own.
    #
    # This is a real claim about behaviour, not a thumb on the scale: people are
    # measurably less likely to like or comment on something a week old, while
    # how much of a video they watch barely depends on its age at all. So
    # freshness multiplies the engagement predictions and leaves completion
    # alone.
    #
    # Floored at 0.3 rather than allowed to reach zero, because an old Swift
    # that everybody finishes is still a good Swift, and a decay with no floor
    # makes the feed unable to say so. Recency reached only `light_score`
    # before this, which meant the heavy ranker was ordering a week-old post
    # with 400 likes above a two-hour-old one that was climbing faster.
    freshness = 0.3 + 0.7 * _recency_decay(f.recency_hours)

    # How much of it they will watch. Population completion rate, lifted by
    # affinity and interest overlap, damped by a history of skipping this
    # creator.
    skip_damping = math.pow(0.75, min(f.creator_negative, 6))
    completion = _clamp01(f.completion_rate * affinity * interest * skip_damping)

    # Watch seconds, expressed against the reference length so the head stays
    # comparable with the probabilities. A long video watched halfway is more
    # watching than a short one watched fully, and this is where that shows up.
    watch_seconds = completion * f.duration_s
    watch_time = _clamp01(watch_seconds / max(load_params().limits.reference_watch_s, 1.0))

    # Taps. Population rates, same adjustments, plus audio familiarity — a
    # viewer who has liked this sound before is measurably more likely to
    # engage with it again.
    audio = 1.0 + f.audio_affinity * 0.25
    like = _clamp01(f.like_rate * affinity * interest * audio * freshness)
    comment = _clamp01(f.comment_rate * affinity * social * freshness)
    repost = _clamp01(f.repost_rate * affinity * freshness)

    # A replay is completion plus enthusiasm; it is only plausible at all for
    # something they finished.
    replay = _clamp01(completion * completion * (affinity - 1.0))

    # Opening a profile is about the creator, not the video.
    profile_open = _clamp01(0.02 * affinity * social * interest * freshness)

    # A reveal is an *interaction* prediction and nothing more. No coordinate
    # is read to produce it, and none is available to this function — see the
    # note in `features.py`.
    location_reveal = _clamp01(0.015 * affinity * interest * freshness)

    # The negative side. Skip is the complement of completion, floored so that
    # a candidate with no history is never scored as certain to be watched.
    skip = _clamp01(1.0 - completion) * 0.9 + 0.05
    negative = _clamp01(min(f.creator_negative, 5) / 5.0)

    return {
        COMPLETION: completion,
        WATCH_TIME: watch_time,
        REPLAY: replay,
        LIKE: like,
        COMMENT: comment,
        REPOST: repost,
        PROFILE_OPEN: profile_open,
        LOCATION_REVEAL: location_reveal,
        SKIP: skip,
        NEGATIVE_FEEDBACK: negative,
        # Set by the diversity pass, which is the only stage that can see a
        # candidate's neighbours.
        CREATOR_FATIGUE: _clamp01(f.creator_repetition / 3.0),
        TOPIC_FATIGUE: _clamp01(f.topic_repetition / 4.0),
        SEEN_BEFORE: 1.0 if f.previously_watched else 0.0,
    }


def aggregate(heads, weights) -> float:
    """Weighted sum of the heads.

    Deliberately a plain sum and not a product or a learned combiner: a sum is
    the one aggregation where "why did this rank here" can be answered by
    listing the terms, which is what `explain()` does and what makes this
    debuggable at all.
    """
    return sum(value * getattr(weights, name) for name, value in heads.items())


@dataclass
class Scored:
    swift_id: int
    score: float
    heads: dict
    features: object

    @property
    def creator_id(self):
        return self.features.creator_id


def heavy_score(features_by_id, *, params=None):
    """Score every hydrated candidate. Returns `Scored`, best first.

    Ties break on swift id, descending. That matters more than it looks:
    without a deterministic tiebreak, two candidates with identical scores swap
    places between requests and pagination stops being stable. Newer id first
    is a sensible tiebreak because ids are monotonic here.
    """
    params = params or load_params()
    weights = params.weights

    scored = []
    for swift_id, f in features_by_id.items():
        heads = estimate_heads(f)
        scored.append(Scored(
            swift_id=swift_id,
            score=aggregate(heads, weights),
            heads=heads,
            features=f,
        ))

    scored.sort(key=lambda s: (-s.score, -s.swift_id))
    return scored


def explain(scored, *, params=None, top=5):
    """The largest contributors to one candidate's score.

    For the debug endpoint and for reading test failures. Returns the term
    values, not the head probabilities, because "completion contributed 3.1"
    answers the question and "completion was 0.39" does not.
    """
    params = params or load_params()
    weights = params.weights
    terms = [
        (name, value * getattr(weights, name))
        for name, value in scored.heads.items()
    ]
    terms.sort(key=lambda t: -abs(t[1]))
    return terms[:top]
