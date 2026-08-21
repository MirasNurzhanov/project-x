"""The recommender, assembled.

    candidate generation      candidates.gather
            ↓
    safety filter             safety.apply           ← permission, once, for all
            ↓
    exposure filter           exposure.apply
            ↓
    light scoring             scoring.light_score    ← the only full-pool stage
            ↓
    feature hydration         features.hydrate       ← 7 queries, any pool size
            ↓
    heavy scoring             scoring.heavy_score
            ↓
    diversity + exploration   diversity.arrange
            ↓
    session cache             cached, then paged

Read top to bottom, that is X's For You pipeline with the Scala removed. The
ordering is not arbitrary and two placements in particular are load-bearing:

**Safety runs before scoring, not after.** Filtering after ranking means the
expensive stages spend their budget on candidates that were never eligible, and
worse, it means the number of results depends on how many got filtered out — a
page of ten becomes a page of four. Filtering first means the funnel narrows
monotonically.

**Light ranking runs before hydration.** Hydration is seven queries regardless
of pool size, but the Python that follows it is linear in candidates. Cutting
400 to 150 first is what keeps the ranked request in single-digit
milliseconds of CPU.

## Sessions, and why the feed is not re-ranked per swipe

Ranking on every page request would mean re-running everything above for each
ten Swifts, and — worse — the answer would change between pages, so a candidate
could appear on page 1 and again on page 3. Instead one ranking pass produces
`limits.session_size` ordered ids, cached under a session token, and pages are
slices of that list.

The token is returned to the client as an opaque cursor. It is the same
mechanism X uses for timeline cursoring, minus the distributed store: here it
is a Redis key with a TTL.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from django.core.cache import cache
from django.utils import timezone

from . import candidates as sources
from . import diversity, exposure, features, metrics, safety, scoring
from .params import load_params

SESSION_PREFIX = 'recs:session:'


@dataclass
class RankedPage:
    swift_ids: list
    sources_by_id: dict
    cursor: str | None
    has_more: bool
    #: Funnel counts per stage. Returned rather than only logged so the debug
    #: endpoint and the tests can assert on them.
    stages: dict = field(default_factory=dict)
    source_counts: dict = field(default_factory=dict)
    distribution: dict = field(default_factory=dict)
    cache_hit: bool = False
    latency_ms: float = 0.0


def _session_key(viewer_id, token):
    return f'{SESSION_PREFIX}{viewer_id}:{token}'


def build_session(viewer, *, params=None, now=None, seed=None):
    """Run the whole pipeline once and return the ordered ids.

    Returns `(ordered_ids, sources_by_id, stages, source_counts)`.
    """
    params = params or load_params()
    now = now or timezone.now()
    limits = params.limits
    stages = {}

    # 1 — candidate generation
    with metrics.timed('recs.stage.generate'):
        pool, source_counts = sources.gather(viewer, params=params, now=now)
    stages['generated'] = len(pool)

    # 2 — safety. Permission, applied once, over everything.
    with metrics.timed('recs.stage.safety'):
        pool, dropped_unsafe = safety.apply(viewer, pool)
        suppressed = safety.suppressed_creator_ids(viewer)
    stages['filtered_visibility'] = dropped_unsafe

    # 3 — exposure
    with metrics.timed('recs.stage.exposure'):
        pool, dropped_seen = exposure.apply(viewer, pool, params=params, now=now)
    stages['filtered_exposure'] = dropped_seen
    stages['eligible'] = len(pool)

    if not pool:
        return [], {}, stages, source_counts

    # 4 — light ranking. Needs the rows, but only the cheap columns.
    with metrics.timed('recs.stage.light'):
        rows = sources.hydrate_rows(pool)
        # Suppressed creators are dropped here rather than in `safety.apply`
        # because it needs the rows to know the author, and fetching them
        # earlier would waste the work on candidates about to be filtered.
        if suppressed:
            before = len(pool)
            pool = [
                c for c in pool
                if c.swift_id in rows and rows[c.swift_id].author_id not in suppressed
            ]
            stages['filtered_suppressed'] = before - len(pool)
        else:
            pool = [c for c in pool if c.swift_id in rows]
            stages['filtered_suppressed'] = 0

        from users import visibility as user_visibility
        friend_ids, _ = user_visibility.approved_ids_for(viewer)
        pool.sort(
            key=lambda c: -scoring.light_score(
                rows[c.swift_id],
                now=now,
                is_friend=rows[c.swift_id].author_id in friend_ids,
                source_agreement=len(c.sources),
            ),
        )
        pool = pool[:limits.light_ranked]
    stages['light_ranked'] = len(pool)

    # 5 — feature hydration
    with metrics.timed('recs.stage.hydrate'):
        feature_map = features.hydrate(
            viewer, pool, rows, params=params, now=now, friend_ids=friend_ids,
        )
    stages['hydrated'] = len(feature_map)

    # 6 — heavy ranking
    with metrics.timed('recs.stage.heavy'):
        scored = scoring.heavy_score(feature_map, params=params)

    # 7 — diversity, quotas and exploration
    with metrics.timed('recs.stage.diversity'):
        scored = _apply_source_ceilings(scored, params)
        audio_by_id = {
            swift_id: (row.audio_apple_id or row.audio_title or '')
            for swift_id, row in rows.items()
        }
        arranged = diversity.arrange(
            scored,
            limit=limits.session_size,
            params=params,
            seed=seed if seed is not None else viewer.pk,
            audio_by_id=audio_by_id,
        )
    stages['arranged'] = len(arranged)

    ordered_ids = [s.swift_id for s in arranged]
    sources_by_id = {
        s.swift_id: _primary_source(s.features.sources)
        for s in arranged
    }
    return ordered_ids, sources_by_id, stages, source_counts


def _primary_source(source_set):
    for source in sources.ALL_SOURCES:
        if source in source_set:
            return source
    return ''


def _apply_source_ceilings(scored, params):
    """Stop any one source owning the session.

    Only the friend graph has a ceiling today, and it is the one that needs it:
    a well-connected viewer's friend Swifts will out-score everything else on
    affinity alone, and a feed that is 100% people you already know is not a
    recommender. Candidates over the ceiling are dropped from *this* session,
    not penalised — they are perfectly good Swifts and will be back.
    """
    ceiling_pct = params.quotas.friends_ceiling_pct
    if ceiling_pct >= 1.0:
        return scored

    ceiling = max(1, int(params.limits.session_size * ceiling_pct))
    kept = []
    friends_taken = 0
    for candidate in scored:
        if sources.FRIENDS in candidate.features.sources:
            if friends_taken >= ceiling:
                continue
            friends_taken += 1
        kept.append(candidate)
    return kept


def page(viewer, *, cursor=None, limit=None, params=None, now=None, seed=None):
    """One page of the ranked feed.

    A cursor of `None` starts a session: ranks, caches, returns the first
    slice. A cursor continues one — a cache read and a list slice, no ranking.
    A cursor whose session has expired silently starts a new one rather than
    erroring, because a viewer whose feed 500s because they paused for half an
    hour is a worse outcome than a feed that reshuffles.
    """
    params = params or load_params()
    now = now or timezone.now()
    limits = params.limits
    limit = min(limit or limits.page_size, limits.max_page_size)

    started = time.perf_counter()
    token, offset = _parse_cursor(cursor)
    session = cache.get(_session_key(viewer.pk, token)) if token else None
    cache_hit = session is not None

    if cache_hit:
        metrics.incr('recs.session.hit')
        ordered_ids = session['ids']
        sources_by_id = session['sources']
        stages = session.get('stages', {})
        source_counts = session.get('source_counts', {})
    else:
        metrics.incr('recs.session.miss')
        ordered_ids, sources_by_id, stages, source_counts = build_session(
            viewer, params=params, now=now, seed=seed,
        )
        token = secrets.token_urlsafe(9)
        offset = 0
        cache.set(
            _session_key(viewer.pk, token),
            {
                'ids': ordered_ids,
                'sources': sources_by_id,
                'stages': stages,
                'source_counts': source_counts,
            },
            timeout=limits.session_ttl_s,
        )

    window = ordered_ids[offset:offset + limit]
    has_more = offset + limit < len(ordered_ids)

    if window:
        exposure.mark_served(
            viewer, [(swift_id, sources_by_id.get(swift_id, '')) for swift_id in window],
        )
        for swift_id in window:
            source = sources_by_id.get(swift_id) or 'unknown'
            metrics.incr(f'recs.served.{source}')

    distribution = {}
    for swift_id in window:
        source = sources_by_id.get(swift_id) or 'unknown'
        distribution[source] = distribution.get(source, 0) + 1

    latency_ms = (time.perf_counter() - started) * 1000.0
    metrics.observe_latency('recs.page', latency_ms)
    metrics.log_ranking(
        viewer.pk,
        stages=stages,
        sources=source_counts,
        distribution=distribution,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        page_size=len(window),
    )

    return RankedPage(
        swift_ids=window,
        sources_by_id={i: sources_by_id.get(i, '') for i in window},
        cursor=_make_cursor(token, offset + limit) if has_more else None,
        has_more=has_more,
        stages=stages,
        source_counts=source_counts,
        distribution=distribution,
        cache_hit=cache_hit,
        latency_ms=latency_ms,
    )


# ── Cursor encoding ───────────────────────────────────────────────────────────
# `token.offset`. Opaque to the client by convention rather than by encryption:
# there is nothing secret in it, and a tampered cursor can only reorder or
# truncate the tamperer's own feed. Every id it can reach was already filtered
# for that viewer before it entered the session.
def _make_cursor(token, offset):
    return f'{token}.{offset}'


def _parse_cursor(cursor):
    if not cursor or '.' not in cursor:
        return None, 0
    token, _, raw_offset = cursor.rpartition('.')
    if not token:
        return None, 0
    try:
        offset = max(0, int(raw_offset))
    except (TypeError, ValueError):
        return None, 0
    return token, offset


def invalidate(viewer):
    """Drop any cached sessions for this viewer.

    There is no key enumeration here — the token is random — so this is a
    marker for the places that would want it (a block, a "not interested") and
    is implemented by letting the TTL do the work. Documented rather than
    faked: a function that claims to invalidate and does not is worse than
    none.
    """
    return False
