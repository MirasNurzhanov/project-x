"""Every number the recommender uses, in one place.

This is the direct analogue of home-mixer's `HomeGlobalParams.ModelWeights` —
X keeps each scoring weight as a named, bounded, individually overridable
parameter rather than a literal in the scorer, and the reason is the one that
matters most here: a weight buried in a function is a weight nobody can tune
without a deploy, and a recommender you cannot tune is a recommender you cannot
fix.

Two rules hold for this module:

* **No magic number appears anywhere else in `recs/`.** If a scorer, a filter
  or a diversity pass needs a constant, it comes from here. That is checkable,
  and `tests/test_params.py` checks it.
* **Everything is overridable from Django settings** under `SWIFT_RECS`, so
  staging can run different weights from production without a code change.
  This is the poor-relation version of X's FeatureSwitch layer, and it is
  enough: the point is that the values live outside the algorithm, not that
  they can be changed per-request.

Weights are deliberately *interpretable*. Each one says "how much is one unit
of this worth relative to the others", and the final score is their sum. There
is no trained model behind them and this module does not pretend otherwise —
see `recs/scoring.py` for what the estimators actually are.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

from django.conf import settings


@dataclass(frozen=True)
class ScoringWeights:
    """What each predicted engagement is worth in the final score.

    Mirrors X's `PredictedScoreFeature` set: one weight per head, summed. The
    heads themselves are estimated deterministically for now (see
    `scoring.py`); swapping in a model changes the estimator and leaves these
    weights, and the code that applies them, exactly as they are.

    Scale: heads are probabilities in [0, 1] or normalised rates in [0, 1], so
    a weight is directly "points of score at full confidence". They are chosen
    so that watching is worth more than any single tap — a feed optimised for
    likes turns into a feed of bait — and so that the whole negative block can
    outweigh the whole positive block, because a Swift somebody will skip is
    worse than no Swift at all.
    """

    # ── Positive heads ────────────────────────────────────────────────────────
    completion: float = 8.0
    """Predicted fraction of the video watched. The single strongest signal a
    short-video feed has, and the hardest to game."""

    watch_time: float = 5.0
    """Predicted watch seconds, normalised by a reference length. Separate from
    completion because a 60s video watched to 50% is more watching than a 5s
    video watched twice over."""

    replay: float = 3.0
    like: float = 2.5
    comment: float = 3.5
    """Above `like` on purpose: a comment costs the viewer more, so it says
    more. Same ordering X uses between fav and reply."""
    repost: float = 4.0
    profile_open: float = 2.0
    location_reveal: float = 1.5
    """A reveal is a strong interest signal — someone wanted to know where this
    was. It is scored as an *interaction*, and nothing about the coordinate
    itself ever enters ranking. See the module note in `features.py`."""

    # ── Negative heads ────────────────────────────────────────────────────────
    # Negative, so they subtract. Kept as separate heads rather than one
    # "badness" number so that the log can say which one fired.
    skip: float = -6.0
    """Predicted probability the viewer bails in the first seconds."""
    negative_feedback: float = -25.0
    """Report / not-interested / block history against this creator. Large
    enough to sink a candidate on its own, which is the intent."""
    creator_fatigue: float = -4.0
    """Same creator seen repeatedly in this session."""
    topic_fatigue: float = -2.0
    seen_before: float = -30.0
    """A Swift already served. Belt and braces — `exposure.py` filters these
    out entirely, so this only ever catches a bug in that filter."""


@dataclass(frozen=True)
class SourceQuotas:
    """How many candidates each source may contribute, and its floor.

    `cap` bounds the query; `floor` is the number of slots reserved in the
    final feed if the source produced anything at all. Floors are what enforce
    the brief's "do not allow one source to completely dominate": without them
    a viewer with 400 friends gets a feed that is 100% friends and never
    discovers anybody, which is precisely the failure X's in-network/out-of-
    network balance exists to prevent.
    """

    friends_cap: int = 120
    friends_floor: int = 0
    """No floor. Friends win slots on merit or not at all — the ceiling below
    is what matters for this source."""
    friends_ceiling_pct: float = 0.6
    """At most 60% of a page from the friend graph, even when it scores best."""

    affinity_cap: int = 80
    affinity_floor: int = 0

    collaborative_cap: int = 80
    collaborative_floor: int = 0

    similarity_cap: int = 60
    similarity_floor: int = 0

    trending_cap: int = 60
    trending_floor: int = 1

    new_creator_cap: int = 40
    new_creator_floor: int = 1
    """Reserved exposure. A new creator's first Swifts are unrankable — no
    engagement history means every rate estimate is a prior — so they get slots
    by reservation rather than by score. This is Aura's version of the
    exploration budget X spends through FRS."""

    cold_start_cap: int = 120
    cold_start_floor: int = 0


@dataclass(frozen=True)
class DiversityRules:
    """Caps applied after scoring, in the final ordering pass."""

    max_per_creator_per_page: int = 2
    min_gap_between_same_creator: int = 3
    """Positions. Two Swifts by one creator may both appear, but not adjacent."""
    max_per_topic_per_page: int = 4
    max_consecutive_same_topic: int = 2
    exploration_rate: float = 0.15
    """Fraction of each page given to candidates that did *not* win on score.
    The exploitation/exploration mix. Drawn deterministically from a seeded RNG
    so a page is reproducible — see `pipeline.py`."""


@dataclass(frozen=True)
class ExposureRules:
    """When a Swift may be shown again."""

    reserve_hours: int = 72
    """A Swift served but never actually watched — the viewer stopped scrolling
    before reaching it — becomes eligible again after this long. Without it,
    over-fetching a page permanently burns candidates nobody ever saw."""
    watched_never_again: bool = True
    """Once genuinely watched (a `SwiftView` row exists), never re-served.
    Rewatching is something a viewer does deliberately from a profile, not
    something the feed should decide for them."""


@dataclass(frozen=True)
class PipelineLimits:
    """Sizes at each stage. The shape of the funnel."""

    candidate_pool: int = 400
    """Everything the sources may contribute, before filtering."""
    light_ranked: int = 150
    """Survivors of light scoring, which is the only stage that runs over the
    whole pool. Everything after this is allowed to be expensive."""
    session_size: int = 60
    """How many ranked ids are cached per feed session. Roughly six pages, so a
    normal browsing run costs one ranking pass rather than one per swipe."""
    session_ttl_s: int = 1800
    page_size: int = 10
    max_page_size: int = 30

    trending_window_hours: int = 48
    new_creator_window_days: int = 14
    new_creator_max_swifts: int = 10
    affinity_half_life_days: float = 14.0
    """Signals decay. A creator you loved two months ago should not outrank one
    you liked yesterday."""
    reference_watch_s: float = 12.0
    """Watch seconds that count as a full unit for the `watch_time` head."""


@dataclass(frozen=True)
class RecsParams:
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    quotas: SourceQuotas = field(default_factory=SourceQuotas)
    diversity: DiversityRules = field(default_factory=DiversityRules)
    exposure: ExposureRules = field(default_factory=ExposureRules)
    limits: PipelineLimits = field(default_factory=PipelineLimits)


def _apply_overrides(instance, overrides):
    """Replace only the fields named, and only if they exist.

    Unknown keys raise rather than being ignored: a typo'd weight in settings
    that silently does nothing is the worst possible failure for a tuning knob,
    because it looks like the weight had no effect.
    """
    known = {f.name for f in fields(instance)}
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(
            f'Unknown SWIFT_RECS parameter(s) for {type(instance).__name__}: '
            f'{", ".join(sorted(unknown))}'
        )
    return replace(instance, **overrides)


def load_params() -> RecsParams:
    """The active parameters, defaults merged with `settings.SWIFT_RECS`.

    Read per call rather than cached at import so tests can
    `override_settings` — the same reason `users.visibility` reads its own
    settings live.
    """
    params = RecsParams()
    overrides = getattr(settings, 'SWIFT_RECS', None) or {}
    if not overrides:
        return params

    section_map = {
        'weights': params.weights,
        'quotas': params.quotas,
        'diversity': params.diversity,
        'exposure': params.exposure,
        'limits': params.limits,
    }
    unknown_sections = set(overrides) - set(section_map)
    if unknown_sections:
        raise ValueError(
            f'Unknown SWIFT_RECS section(s): {", ".join(sorted(unknown_sections))}'
        )

    updated = {
        name: _apply_overrides(section, overrides.get(name, {}))
        for name, section in section_map.items()
    }
    return replace(params, **updated)
