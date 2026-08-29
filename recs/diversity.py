"""Making a feed out of a sorted list.

A ranked list is not a feed. The ten highest-scoring candidates for somebody
who loves one creator are ten Swifts by that creator, and every one of them is
individually the right answer. The feed is still wrong.

## The technique

X solves this with Maximal Marginal Relevance over learned embeddings — pick
greedily, but penalise each candidate by its similarity to what has already
been picked. This is MMR with a similarity function Approx can actually compute:

    same creator      1.0
    same audio track  0.6
    shared interests  up to 0.4, proportional

No embeddings, no model, no vector store. The structure is identical and can be
swapped to cosine distance over real embeddings later by replacing `_similar`.

On top of MMR sit two hard rules, because a soft penalty is not a guarantee and
"never ten in a row by one creator" is a guarantee:

    at most `max_per_creator_per_page`, and never within
    `min_gap_between_same_creator` positions of each other
    at most `max_consecutive_same_topic` in a row

## Exploration

`exploration_rate` of each page is drawn from candidates that did *not* win on
score, sampled from the tail. Without it the feed converges: the ranker only
ever learns about what it already believes, because those are the only Swifts
it ever shows.

The draw is seeded from `(viewer_id, session)` rather than left to system
randomness, so a page is reproducible — the same request twice returns the same
feed, which is what makes pagination stable and the tests deterministic.
"""
from __future__ import annotations

import random

from .params import load_params


def _topic_key(features, topics_by_creator):
    """The topic bucket a candidate belongs to.

    The creator's first interest category. Coarse on purpose: the goal is to
    stop a page being all one *kind* of thing, and finer buckets stop catching
    that. A creator with no interests gets their own bucket rather than sharing
    a null one, so the untagged do not all collide.
    """
    topics = topics_by_creator.get(features.creator_id)
    if topics:
        return f'c{sorted(topics)[0]}'
    return f'u{features.creator_id}'


def _similar(a, b, topics_by_creator):
    """How much two candidates are 'the same thing', in [0, 1]."""
    if a.features.creator_id == b.features.creator_id:
        return 1.0

    score = 0.0
    a_audio = getattr(a, 'audio_key', '')
    b_audio = getattr(b, 'audio_key', '')
    if a_audio and a_audio == b_audio:
        score += 0.6

    a_topics = topics_by_creator.get(a.features.creator_id) or set()
    b_topics = topics_by_creator.get(b.features.creator_id) or set()
    if a_topics and b_topics:
        overlap = len(a_topics & b_topics) / max(len(a_topics | b_topics), 1)
        score += overlap * 0.4

    return min(score, 1.0)


def topics_for(creator_ids):
    """Interest-category ids per creator, in one query.

    Categories rather than individual interests: two creators who both like
    "bouldering" and "trail running" are the same topic for feed purposes, and
    the category column already encodes that.
    """
    if not creator_ids:
        return {}
    from django.contrib.auth import get_user_model
    User = get_user_model()

    out: dict[int, set] = {}
    rows = (
        User.objects
        .filter(id__in=list(creator_ids))
        .values_list('id', 'interests__category')
    )
    for user_id, category in rows:
        if category:
            out.setdefault(user_id, set()).add(category)
    return out


def arrange(scored, *, limit, params=None, seed=0, audio_by_id=None, window=None):
    """Greedy MMR with hard caps, then exploration.

    Returns the arranged list, at most `limit` long.

    Greedy rather than optimal because optimal ordering under these constraints
    is a scheduling problem, and the difference is invisible in a feed of ten
    while the cost is not.
    """
    params = params or load_params()
    rules = params.diversity
    if not scored:
        return []

    # The caps are *per page*, and a session is several pages. Applying them
    # once across the whole session was a real bug and an instructive one: with
    # `max_per_creator_per_page = 2`, a session of sixty drawn from three
    # creators could contain six Swifts and then stop, and the feed would end
    # after half a screen for reasons no filter counted. Counts therefore reset
    # every `window` positions, which is what "per page" meant all along.
    window = window or params.limits.page_size

    audio_by_id = audio_by_id or {}
    for s in scored:
        s.audio_key = audio_by_id.get(s.swift_id, '')

    topics = topics_for({s.features.creator_id for s in scored})

    # ── Exploration budget ────────────────────────────────────────────────────
    # Taken from the *tail* — candidates that survived every filter but did not
    # win on score. Drawing from the head would not be exploration, and drawing
    # from outside the filtered set would not be safe.
    explore_slots = int(limit * rules.exploration_rate)
    rng = random.Random(seed)
    explore_picks = []
    if explore_slots > 0 and len(scored) > limit:
        tail = scored[limit:]
        explore_picks = rng.sample(tail, min(explore_slots, len(tail)))

    pool = list(scored)
    chosen = []
    creator_counts: dict[int, int] = {}
    topic_counts: dict[str, int] = {}
    last_positions: dict[int, int] = {}

    def clears_gap(candidate, position):
        """Far enough from the last Swift by the same creator.

        Positional, and deliberately *not* reset at a window boundary: the
        point is that two by one creator must not sit next to each other, and a
        page break is not a reason they may.
        """
        last = last_positions.get(candidate.features.creator_id)
        return last is None or position - last >= rules.min_gap_between_same_creator

    def admissible(candidate, position):
        creator = candidate.features.creator_id
        if creator_counts.get(creator, 0) >= rules.max_per_creator_per_page:
            return False
        if not clears_gap(candidate, position):
            return False

        topic = _topic_key(candidate.features, topics)
        if topic_counts.get(topic, 0) >= rules.max_per_topic_per_page:
            return False
        run = 0
        for previous in reversed(chosen):
            if _topic_key(previous.features, topics) == topic:
                run += 1
            else:
                break
        return run < rules.max_consecutive_same_topic

    # MMR's penalty is "how similar is this to anything already picked", which
    # reads as a nested loop and was written as one — O(picks × pool × chosen),
    # and at 150 candidates it was the single most expensive stage in the
    # pipeline, costing more than every database query put together.
    #
    # It does not need to be. The penalty is a running maximum, so it can be
    # carried forward: each pick updates every remaining candidate once, which
    # is O(picks × pool) with the same result to the last decimal.
    penalty_by_id: dict[int, float] = {c.swift_id: 0.0 for c in scored}

    def take(candidate, position):
        creator = candidate.features.creator_id
        creator_counts[creator] = creator_counts.get(creator, 0) + 1
        last_positions[creator] = position
        topic = _topic_key(candidate.features, topics)
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        chosen.append(candidate)
        for other in pool:
            if other.swift_id == candidate.swift_id:
                continue
            similarity = _similar(other, candidate, topics)
            if similarity > penalty_by_id[other.swift_id]:
                penalty_by_id[other.swift_id] = similarity

    explore_ids = {c.swift_id for c in explore_picks}
    explore_at = set(rng.sample(range(limit), len(explore_picks))) if explore_picks else set()
    explore_queue = list(explore_picks)

    while len(chosen) < limit and (pool or explore_queue):
        # Position *is* the output index, so a window boundary is exactly a
        # page boundary for the client. Deriving it from a separate counter
        # let the two drift, and a "per page" cap that resets somewhere other
        # than a page break is not the cap anybody meant.
        position = len(chosen)
        if position and position % window == 0:
            creator_counts.clear()
            topic_counts.clear()

        # An exploration slot draws from the tail, but still has to clear the
        # diversity rules — exploration is not licence to bunch three of one
        # creator together.
        if position in explore_at and explore_queue:
            picked = next(
                (c for c in explore_queue if admissible(c, position)), None,
            )
            if picked is not None:
                explore_queue.remove(picked)
                pool[:] = [c for c in pool if c.swift_id != picked.swift_id]
                take(picked, position)
                continue

        best = None
        best_value = None
        for candidate in pool:
            if candidate.swift_id in explore_ids:
                continue
            if not admissible(candidate, position):
                continue
            value = candidate.score * (1.0 - penalty_by_id[candidate.swift_id] * 0.5)
            if best_value is None or value > best_value:
                best, best_value = candidate, value

        if best is None:
            # Every remaining candidate violates a cap.
            #
            # With a narrow creator pool this is normal rather than
            # exceptional: a viewer whose only two creators have twenty Swifts
            # between them should get twenty of them, spaced out — not four and
            # then an empty feed. Truncating here is the worse failure, and it
            # is silent, so the caps yield instead.
            #
            # They yield in order. Candidates that still clear the adjacency
            # gap go first, so the last thing given up is the one rule that was
            # ever about what the feed *looks* like; the per-window ceiling,
            # which is about proportion, goes before it.
            if not pool:
                break
            best = max(pool, key=lambda c: (clears_gap(c, position), c.score))

        pool[:] = [c for c in pool if c.swift_id != best.swift_id]
        take(best, position)

    # The counts the scorer wanted but could not have: how many of this
    # creator, and this topic, ended up on the page. Written back so a
    # subsequent page can see them and so `explain()` can report them.
    for candidate in chosen:
        candidate.features.creator_repetition = creator_counts.get(
            candidate.features.creator_id, 1,
        ) - 1
        candidate.features.topic_repetition = topic_counts.get(
            _topic_key(candidate.features, topics), 1,
        ) - 1

    return chosen


def source_distribution(scored):
    """How many of the arranged candidates came from each source.

    The observability metric the brief asks for, computed here because this is
    the last stage that has the candidate objects.
    """
    out: dict[str, int] = {}
    for candidate in scored:
        for source in candidate.features.sources:
            out[source] = out.get(source, 0) + 1
    return out
