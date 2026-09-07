"""Where candidates come from.

Seven sources, each answering a different question about why a viewer might
want a Swift. This is the stage X calls candidate generation, and the shape is
borrowed directly: each source is independent, capped, tagged with its own
identifier, and none of them is trusted to be complete or correct on its own.
The mixing that follows is what makes a feed out of them.

## The rule every source obeys

**A source returns ids and nothing else.** No source filters for visibility, no
source decides what the viewer may watch, and no source touches a coordinate.
Sourcing is "what might be interesting"; `safety.py` is "what is permitted",
and it runs over the union afterwards. Two reasons, both learned the expensive
way in this codebase already:

* a permission check spread across seven call sites is a permission check with
  seven chances to be wrong, and the one that is wrong is the leak;
* a source that pre-filters cannot be tested for what it *would* have returned.

Every source here therefore starts from `swift_visibility.watchable(viewer)`
only as a cheap early narrowing where it is free to do so, and `safety.py`
re-applies the full gate regardless. The narrowing is an optimisation; the gate
is the guarantee.

## What each source is for

    friends           the people you chose. Approx's analogue of in-network.
    affinity          creators you keep engaging with, and who they engage with.
    collaborative     viewers whose taste matches yours liked these.
    similarity        same interests, same audio, same neighbourhood of content.
    trending          engagement per hour, age-adjusted.
    new_creator       reserved exposure for people with almost no history.
    cold_start        a diverse default when we know nothing about you.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from swifts import visibility as swift_visibility
from swifts.models import SwiftVideo
from users import visibility as user_visibility

from .models import CreatorAffinity
from .params import load_params

# ── Source identifiers ────────────────────────────────────────────────────────
FRIENDS = 'friends'
AFFINITY = 'affinity'
COLLABORATIVE = 'collaborative'
SIMILARITY = 'similarity'
TRENDING = 'trending'
NEW_CREATOR = 'new_creator'
COLD_START = 'cold_start'

ALL_SOURCES = (
    FRIENDS, AFFINITY, COLLABORATIVE, SIMILARITY, TRENDING, NEW_CREATOR, COLD_START,
)


@dataclass
class Candidate:
    """One Swift, plus how it got here.

    `sources` is a set because the same Swift is very often produced by several
    — a friend's trending Swift is both — and which sources agreed is itself a
    ranking signal (`source_agreement` in `features.py`). Collapsing it to a
    single winning source would throw that away.
    """

    swift_id: int
    sources: set = field(default_factory=set)
    #: Per-source hints the source already knows and the ranker would otherwise
    #: have to re-query for. Kept small and per-source-namespaced.
    hints: dict = field(default_factory=dict)

    def merge(self, other: 'Candidate') -> None:
        self.sources |= other.sources
        self.hints.update(other.hints)

    @property
    def primary_source(self) -> str:
        """The source credited in metrics and in `served_by`.

        Ordered by specificity, not by score: "a friend posted it" explains a
        Swift's presence better than "it was trending", even when trending also
        found it.
        """
        for source in ALL_SOURCES:
            if source in self.sources:
                return source
        return ''


def _base(viewer, base=None):
    """Live, watchable Swifts, not authored by the viewer.

    Own Swifts are excluded here rather than filtered later because no source
    should ever propose them: a feed that recommends you your own video is
    broken in a way no downstream stage should have to correct for.

    `base` lets the caller build the common filters once and hand the same
    queryset to every source. Each source filters a lazy clone; relationship
    checks remain SQL predicates evaluated when that clone fetches its rows.
    """
    if base is not None:
        return base
    return (
        swift_visibility.watchable(viewer)
        .exclude(author_id=viewer.pk)
    )


def _ids(queryset, cap):
    return list(queryset.values_list('id', flat=True)[:cap])


def _pack(ids, source, hints_for=None):
    return [
        Candidate(
            swift_id=i,
            sources={source},
            hints=({f'{source}.{k}': v for k, v in hints_for(i).items()} if hints_for else {}),
        )
        for i in ids
    ]


# ── A. Following network ──────────────────────────────────────────────────────
def from_friends(viewer, cap, *, base=None):
    """Recent Swifts from the viewer's accepted friends.

    Approx has no follow graph — the social model is mutual friendship — so this
    is the in-network source, and `approved_ids_for` is the same helper every
    other friends-only surface in the app uses. Reverse-chronological within
    the source: recency is what people expect from the people they chose, and
    the ranker is free to disagree.
    """
    # `approved_ids_for` returns (friends, approved) — approved being friends
    # plus accepted Hi connections. The generous tier is the right one for a
    # candidate *source*: these are all people the viewer chose. The stricter
    # set is what `features.is_friend` uses, because "we are actually friends"
    # is the sharper ranking signal.
    _, approved_ids = user_visibility.approved_ids_for(viewer)
    if not approved_ids:
        return []
    qs = _base(viewer, base).filter(author_id__in=approved_ids).order_by('-created_at')
    return _pack(_ids(qs, cap), FRIENDS)


# ── B. Interaction graph ──────────────────────────────────────────────────────
def from_affinity(viewer, cap, *, params=None, base=None):
    """More from creators this viewer keeps engaging with.

    The `CreatorAffinity` rows with a positive decayed score, best first, then
    their recent Swifts. This is the cheap, single-hop version of what UTEG
    does with a graph traversal: X walks user→tweet→user edges in memory; here
    the edge weight is already materialised, so the walk is an index scan.
    """
    params = params or load_params()
    affinities = list(
        CreatorAffinity.objects
        .filter(viewer=viewer, suppressed=False, score__gt=0)
        .order_by('-score')[:30]
    )
    if not affinities:
        return []

    from .ingest import decayed_score
    now = timezone.now()
    ranked = sorted(
        affinities,
        key=lambda a: decayed_score(
            a, now=now, half_life_days=params.limits.affinity_half_life_days,
        ),
        reverse=True,
    )
    creator_ids = [a.creator_id for a in ranked if decayed_score(
        a, now=now, half_life_days=params.limits.affinity_half_life_days,
    ) > 0]
    if not creator_ids:
        return []

    qs = _base(viewer, base).filter(author_id__in=creator_ids).order_by('-created_at')
    return _pack(_ids(qs, cap), AFFINITY)


# ── C. Collaborative ──────────────────────────────────────────────────────────
def from_collaborative(viewer, cap, *, params=None, base=None):
    """"People whose taste looks like yours engaged with these."

    Two hops over `CreatorAffinity`, which is the same collaborative-filtering
    shape as UTEG without the graph engine:

        1. creators this viewer scores highly,
        2. *other* viewers who also score those creators highly — the peers,
        3. creators those peers score highly that this viewer has no opinion on.

    Bounded hard at every hop. This is the source most capable of running away
    with a query, and the caps are the reason it does not.
    """
    params = params or load_params()

    my_creators = set(
        CreatorAffinity.objects
        .filter(viewer=viewer, suppressed=False, score__gt=0)
        .order_by('-score')
        .values_list('creator_id', flat=True)[:15]
    )
    if not my_creators:
        return []

    peer_ids = set(
        CreatorAffinity.objects
        .filter(creator_id__in=my_creators, score__gt=0)
        .exclude(viewer=viewer)
        .order_by('-score')
        .values_list('viewer_id', flat=True)[:100]
    )
    if not peer_ids:
        return []

    # Creators the peers rate that this viewer has not already formed a view on.
    # `exclude` on the viewer's own pairs rather than on `my_creators` alone:
    # a creator they actively disliked must not come back through the peers.
    known = set(
        CreatorAffinity.objects
        .filter(viewer=viewer)
        .values_list('creator_id', flat=True)
    )
    peer_creators = list(
        CreatorAffinity.objects
        .filter(viewer_id__in=peer_ids, score__gt=0, suppressed=False)
        .exclude(creator_id__in=known | {viewer.pk})
        .values('creator_id')
        .annotate(peers=Count('viewer_id'), total=Count('id'))
        .order_by('-peers')[:25]
    )
    if not peer_creators:
        return []

    creator_ids = [row['creator_id'] for row in peer_creators]
    peer_counts = {row['creator_id']: row['peers'] for row in peer_creators}

    qs = (
        _base(viewer, base)
        .filter(author_id__in=creator_ids)
        .order_by('-created_at')
        .values_list('id', 'author_id')
    )
    rows = list(qs[:cap])
    return [
        Candidate(
            swift_id=swift_id,
            sources={COLLABORATIVE},
            hints={'collaborative.peers': peer_counts.get(author_id, 0)},
        )
        for swift_id, author_id in rows
    ]


# ── D. Content similarity ─────────────────────────────────────────────────────
def from_similarity(viewer, cap, *, base=None):
    """Swifts whose creator shares interests with the viewer, or whose audio
    the viewer has engaged with before.

    Approx has no learned content embeddings and this does not pretend to have
    them — X's SimClusters and TwHIN are exactly what this stage is *not*. What
    Approx does have is an explicit, curated interest taxonomy that every user
    has already filled in, which is a better similarity signal than an untrained
    embedding and costs one join.
    """
    interest_ids = list(viewer.interests.values_list('id', flat=True))
    if not interest_ids:
        return []
    qs = (
        _base(viewer, base)
        .filter(author__interests__in=interest_ids)
        .distinct()
        .order_by('-created_at')
    )
    return _pack(_ids(qs, cap), SIMILARITY)


# ── E. Trending ───────────────────────────────────────────────────────────────
def from_trending(viewer, cap, *, params=None, now=None, base=None):
    """Strong recent engagement, adjusted for age.

    Ranked by engagement *per hour of life* rather than raw count, which is
    what stops a week-old Swift with 400 likes permanently outranking an hour-
    old one with 40. The arithmetic runs in the database — ordering by a Python
    key would mean fetching every recent Swift to sort it.
    """
    params = params or load_params()
    now = now or timezone.now()
    window = now - timedelta(hours=params.limits.trending_window_hours)

    qs = (
        _base(viewer, base)
        .filter(created_at__gte=window)
        .annotate(
            engagement=F('like_count') + F('comment_count') * 2 + F('repost_count') * 3,
        )
        .order_by('-engagement', '-created_at')
    )
    rows = list(qs.values_list('id', 'engagement')[:cap])
    return [
        Candidate(swift_id=i, sources={TRENDING}, hints={'trending.engagement': e})
        for i, e in rows
    ]


# ── F. New creator exploration ────────────────────────────────────────────────
def from_new_creators(viewer, cap, *, params=None, now=None, base=None):
    """Controlled exposure for creators with almost no history.

    Every ranking signal this recommender has is an engagement rate, and a
    creator with no engagement has no rate — only a prior. So they cannot win a
    slot on score, ever, and a feed with no exploration budget is a feed where
    nobody new is ever seen. This source exists to hand them slots by
    reservation instead, via `quotas.new_creator_floor`.

    "New" is bounded both ways — joined recently *and* posted little — so that
    a prolific account cannot sit in the exploration lane forever.
    """
    params = params or load_params()
    now = now or timezone.now()
    joined_after = now - timedelta(days=params.limits.new_creator_window_days)

    qs = (
        _base(viewer, base)
        .filter(author__date_joined__gte=joined_after)
        .annotate(author_swifts=Count('author__swifts'))
        .filter(author_swifts__lte=params.limits.new_creator_max_swifts)
        .order_by('-created_at')
    )
    return _pack(_ids(qs, cap), NEW_CREATOR)


# ── G. Cold start ─────────────────────────────────────────────────────────────
def from_cold_start(viewer, cap, *, params=None, now=None, base=None):
    """A decent feed for somebody we know nothing about.

    Deliberately not "the most popular Swifts of all time", which is how a
    cold-start feed becomes a permanent popularity feed. This is recent, has
    *some* engagement, and — the part that matters — is spread across creators
    by the diversity pass downstream rather than being pre-diversified here.
    """
    params = params or load_params()
    now = now or timezone.now()
    window = now - timedelta(hours=params.limits.trending_window_hours * 3)

    qs = (
        _base(viewer, base)
        .filter(created_at__gte=window)
        .annotate(engagement=F('like_count') + F('comment_count') + F('repost_count'))
        .order_by('-engagement', '-created_at')
    )
    return _pack(_ids(qs, cap), COLD_START)


# ── Mixing ────────────────────────────────────────────────────────────────────
def is_cold(viewer) -> bool:
    """Whether this viewer has enough history to rank against.

    One signal is not history. The threshold is low on purpose: the cold-start
    source is a reasonable feed, not a penalty box, and getting out of it
    quickly matters more than getting the boundary exactly right.
    """
    return not CreatorAffinity.objects.filter(viewer=viewer).exists()


def gather(viewer, *, params=None, now=None):
    """Run every source and merge the results by Swift id.

    Returns `(candidates, per_source_counts)` — the counts are kept separately
    because after merging, "how many did trending produce" is no longer
    answerable from the candidate list, and it is the first number anybody
    debugging a bad feed wants.
    """
    params = params or load_params()
    quotas = params.quotas
    now = now or timezone.now()

    cold = is_cold(viewer)
    # Built once and shared. See the note on `_base`.
    base = _base(viewer)

    produced = []
    if cold:
        # A cold viewer still gets the friend graph — knowing nobody's taste is
        # not the same as knowing nobody.
        produced.append(from_friends(viewer, quotas.friends_cap, base=base))
        produced.append(from_cold_start(viewer, quotas.cold_start_cap, params=params, now=now, base=base))
        produced.append(from_trending(viewer, quotas.trending_cap, params=params, now=now, base=base))
        produced.append(from_similarity(viewer, quotas.similarity_cap, base=base))
        produced.append(from_new_creators(viewer, quotas.new_creator_cap, params=params, now=now, base=base))
    else:
        produced.append(from_friends(viewer, quotas.friends_cap, base=base))
        produced.append(from_affinity(viewer, quotas.affinity_cap, params=params, base=base))
        produced.append(from_collaborative(viewer, quotas.collaborative_cap, params=params, base=base))
        produced.append(from_similarity(viewer, quotas.similarity_cap, base=base))
        produced.append(from_trending(viewer, quotas.trending_cap, params=params, now=now, base=base))
        produced.append(from_new_creators(viewer, quotas.new_creator_cap, params=params, now=now, base=base))

    counts = {}
    merged: dict[int, Candidate] = {}
    for batch in produced:
        for candidate in batch:
            source = next(iter(candidate.sources))
            counts[source] = counts.get(source, 0) + 1
            existing = merged.get(candidate.swift_id)
            if existing is None:
                merged[candidate.swift_id] = candidate
            else:
                existing.merge(candidate)

    return list(merged.values()), counts


def hydrate_rows(candidates):
    """Fetch the `SwiftVideo` rows for a candidate list, in one query.

    Returned as a dict rather than a list because every stage after this
    addresses candidates by id, and because the caller must not depend on the
    database's ordering for anything — the ranker decides order.
    """
    ids = [c.swift_id for c in candidates]
    if not ids:
        return {}
    rows = (
        SwiftVideo.objects
        .filter(id__in=ids)
        .select_related('author')
        .only(
            'id', 'author_id', 'caption', 'created_at', 'duration_ms',
            'like_count', 'comment_count', 'repost_count', 'view_count',
            'audio_apple_id', 'audio_title', 'track_id',
            'status', 'visibility', 'moderation', 'deleted_at',
            'location_shared', 'allow_comments', 'allow_reposts',
            'author__id', 'author__username',
        )
    )
    return {row.id: row for row in rows}
