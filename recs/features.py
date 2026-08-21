"""Everything the scorer is allowed to know about a candidate.

X calls this feature hydration and runs about 6,000 features through it. This
runs 16, and the important structural property is the same one: **hydration is
bulk**. Every feature for every candidate is fetched in a fixed number of
queries that does not grow with the candidate count. A per-candidate lookup
here is how a recommender becomes a 400-query request, and it is the single
easiest mistake to make in this file.

The count is asserted, not hoped for — `tests/test_query_budget.py` pins it.

## The features, and why each is here

    completion_rate       how much of this Swift people finish. The strongest
                          predictor short video has.
    engagement_rate       likes+comments+reposts per viewer reached.
    like_rate / comment_rate / repost_rate
                          separately, because they predict different things.
    velocity              engagement per hour of life. Trending, per-candidate.
    recency_hours         age. Fed to a decay, not used raw.
    creator_affinity      this viewer's decayed score for this creator.
    creator_negative      their skip streak against this creator.
    is_friend             in-network.
    shared_interests      how many interest tags the viewer and creator share.
    audio_affinity        whether the viewer has engaged with this audio before.
    source_agreement      how many independent sources proposed this.
    peer_endorsement      how many taste-peers rate this creator (collaborative).
    creator_is_new        exploration flag.
    previously_watched    a belt-and-braces duplicate of the exposure filter.
    duration_s            length, for the watch-time estimate.

## Location

Nothing here reads a coordinate. `location_shared` is not even hydrated —
whether a Swift has a reveal button is not evidence about whether it is worth
watching, and hydrating it would put a location column inside a feature vector
that gets logged. See `safety.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from swifts.models import SwiftVideo
from users import visibility as user_visibility

from .ingest import decayed_score
from .models import CreatorAffinity, SwiftSignal
from .params import load_params

#: A creator with fewer reached viewers than this has no meaningful rate, so
#: every rate feature is blended toward the prior. This is Bayesian smoothing
#: in its simplest form and it is what stops a Swift with one view and one like
#: reading as a 100% like rate.
PRIOR_STRENGTH = 20.0
PRIOR_COMPLETION = 0.45
PRIOR_LIKE = 0.06
PRIOR_COMMENT = 0.01
PRIOR_REPOST = 0.005


def _smoothed(successes, trials, prior, strength=PRIOR_STRENGTH):
    """A rate you can compare across candidates with wildly different exposure."""
    return (successes + prior * strength) / (trials + strength)


@dataclass
class Features:
    """One candidate's feature vector.

    A dataclass rather than a dict so that a typo is an AttributeError at the
    call site instead of a silently missing feature that scores as zero — which
    is the failure mode that makes ranking bugs so hard to see.
    """

    swift_id: int
    creator_id: int

    completion_rate: float = PRIOR_COMPLETION
    engagement_rate: float = 0.0
    like_rate: float = PRIOR_LIKE
    comment_rate: float = PRIOR_COMMENT
    repost_rate: float = PRIOR_REPOST
    velocity: float = 0.0
    recency_hours: float = 0.0
    duration_s: float = 0.0

    creator_affinity: float = 0.0
    creator_negative: int = 0
    is_friend: bool = False
    shared_interests: int = 0
    audio_affinity: float = 0.0

    source_agreement: int = 1
    peer_endorsement: int = 0
    creator_is_new: bool = False
    previously_watched: bool = False

    #: Filled by the diversity pass, not by hydration — they depend on the
    #: candidates around this one rather than on the candidate itself.
    creator_repetition: int = 0
    topic_repetition: int = 0

    sources: set = field(default_factory=set)


def hydrate(viewer, candidates, rows, *, params=None, now=None, friend_ids=None):
    """Build a `Features` for every candidate, in a fixed number of queries.

    Query budget, and what each one buys:

        1  view aggregates per swift        completion_rate
        2  creator affinities               creator_affinity, creator_negative
        3  friend ids                       is_friend  (skipped when passed in)
        4  shared interest counts           shared_interests
        5  audio engagement                 audio_affinity
        6  new-creator set                  creator_is_new
        7  already-watched set              previously_watched

    Seven *lookups*; the statement count is a little higher because step 3 is
    itself three queries inside `users.visibility` when the caller has not
    already done it. The number that matters, and the one
    `tests/test_query_budget.py` pins, is that none of them depends on how many
    candidates there are — 10 or 400 costs the same.
    """
    params = params or load_params()
    now = now or timezone.now()

    if not candidates:
        return {}

    swift_ids = [c.swift_id for c in candidates]
    creator_ids = {rows[c.swift_id].author_id for c in candidates if c.swift_id in rows}

    # 1 — how much of each Swift people actually finish.
    from swifts.models import SwiftView
    view_stats = {
        row['swift_id']: row
        for row in SwiftView.objects
        .filter(swift_id__in=swift_ids)
        .values('swift_id')
        .annotate(viewers=Count('id'), completions=Count('id', filter=Q(completed=True)))
    }

    # 2 — this viewer's history with each creator.
    affinities = {
        a.creator_id: a
        for a in CreatorAffinity.objects.filter(viewer=viewer, creator_id__in=creator_ids)
    }

    # 3 — the friend graph, once. The strict set: `is_friend` is a ranking
    # signal about a mutual relationship, not about reachability.
    #
    # Taken from the caller when it has one. `approved_ids_for` is three
    # queries, and the pipeline has already paid for them in light ranking —
    # recomputing here made a fixed-cost stage cost three more for no new
    # information.
    if friend_ids is None:
        friend_ids, _ = user_visibility.approved_ids_for(viewer)

    # 4 — interest overlap, computed in the database.
    my_interests = set(viewer.interests.values_list('id', flat=True))
    shared_counts = {}
    if my_interests and creator_ids:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        shared_counts = {
            row['id']: row['shared']
            for row in User.objects
            .filter(id__in=creator_ids)
            .annotate(shared=Count('interests', filter=Q(interests__in=my_interests)))
            .values('id', 'shared')
        }

    # 5 — audio the viewer has already engaged with positively.
    liked_audio = set(
        SwiftSignal.objects
        .filter(
            viewer=viewer,
            kind__in=[SwiftSignal.Kind.LIKE, SwiftSignal.Kind.REPLAY,
                      SwiftSignal.Kind.COMPLETE, SwiftSignal.Kind.REPOST],
            swift__audio_apple_id__gt='',
        )
        .values_list('swift__audio_apple_id', flat=True)[:200]
    )

    # 6 — which creators are inside the exploration window.
    joined_after = now - timedelta(days=params.limits.new_creator_window_days)
    new_creators = set()
    if creator_ids:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        new_creators = set(
            User.objects
            .filter(id__in=creator_ids, date_joined__gte=joined_after)
            .annotate(swift_total=Count('swifts'))
            .filter(swift_total__lte=params.limits.new_creator_max_swifts)
            .values_list('id', flat=True)
        )

    # 7 — belt and braces against the exposure filter.
    from .exposure import watched_ids
    already_watched = watched_ids(viewer, swift_ids)

    out = {}
    for candidate in candidates:
        row = rows.get(candidate.swift_id)
        if row is None:
            continue
        out[candidate.swift_id] = _build(
            candidate, row, now, params,
            view_stats=view_stats,
            affinities=affinities,
            friend_ids=friend_ids,
            shared_counts=shared_counts,
            liked_audio=liked_audio,
            new_creators=new_creators,
            already_watched=already_watched,
        )
    return out


def _build(candidate, row, now, params, **ctx):
    age_hours = max(0.0, (now - row.created_at).total_seconds() / 3600.0)
    reached = max(row.view_count, 1)

    stats = ctx['view_stats'].get(row.id)
    if stats and stats['viewers']:
        completion_rate = _smoothed(
            stats['completions'], stats['viewers'], PRIOR_COMPLETION,
        )
    else:
        completion_rate = PRIOR_COMPLETION

    engagement = row.like_count + row.comment_count + row.repost_count

    affinity = ctx['affinities'].get(row.author_id)
    affinity_score = 0.0
    negative = 0
    if affinity is not None:
        affinity_score = decayed_score(
            affinity, now=now, half_life_days=params.limits.affinity_half_life_days,
        )
        negative = affinity.skip_streak

    audio_affinity = 0.0
    if row.audio_apple_id and row.audio_apple_id in ctx['liked_audio']:
        audio_affinity = 1.0

    return Features(
        swift_id=row.id,
        creator_id=row.author_id,
        completion_rate=completion_rate,
        engagement_rate=engagement / reached,
        like_rate=_smoothed(row.like_count, reached, PRIOR_LIKE),
        comment_rate=_smoothed(row.comment_count, reached, PRIOR_COMMENT),
        repost_rate=_smoothed(row.repost_count, reached, PRIOR_REPOST),
        # +2 rather than +1: a Swift minutes old would otherwise show an
        # enormous velocity off a single like and dominate every page.
        velocity=engagement / (age_hours + 2.0),
        recency_hours=age_hours,
        duration_s=row.duration_ms / 1000.0,
        creator_affinity=affinity_score,
        creator_negative=negative,
        is_friend=row.author_id in ctx['friend_ids'],
        shared_interests=ctx['shared_counts'].get(row.author_id, 0),
        audio_affinity=audio_affinity,
        source_agreement=len(candidate.sources),
        peer_endorsement=int(candidate.hints.get('collaborative.peers', 0)),
        creator_is_new=row.author_id in ctx['new_creators'],
        previously_watched=row.id in ctx['already_watched'],
        sources=set(candidate.sources),
    )
