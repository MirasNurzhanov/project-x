"""Answering "why did the feed do that" after the fact.

Two sinks, deliberately:

* **counters in the cache**, which is Redis in production and locmem in tests.
  Cheap `incr`s, read back by the debug endpoint. These answer the aggregate
  questions — cache hit rate, per-source distribution over time, latency
  buckets.
* **one structured log line per ranked request**, which answers the specific
  question "what happened for this user at this moment", the one aggregates
  can never answer.

## What is never logged

No coordinates. Not the viewer's, not a Swift's, not a rounded or fuzzed
version of either. `recs` never loads those columns (`safety.py`), so there is
nothing here to leak — but the rule is written down because the natural next
feature request for a log line like this is "add where they were", and the
answer is no.

Nor is caption text or username: a feed log is not a content archive, and ids
are sufficient to reconstruct anything needed.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from django.core.cache import cache

logger = logging.getLogger('aura.recs')

#: One namespace so a flush can find all of it, and so the debug endpoint can
#: enumerate without guessing.
PREFIX = 'recs:metric:'

#: Latency buckets, milliseconds. Coarse — the question is "did this get slow",
#: not "what is p99.9 to three digits".
LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500)


def _key(name):
    return f'{PREFIX}{name}'


def incr(name, amount=1):
    """Increment a counter. Never raises — a metric must not fail a request.

    `cache.incr` raises when the key is absent, which is the common case on a
    cold cache, so the miss is caught and seeded rather than pre-checked. Two
    round trips on first write, one thereafter.
    """
    try:
        cache.incr(_key(name), amount)
    except ValueError:
        try:
            cache.add(_key(name), amount, timeout=None)
        except Exception:  # pragma: no cover - cache backend down
            pass
    except Exception:  # pragma: no cover - cache backend down
        pass


def observe_latency(name, elapsed_ms):
    """Record a latency into a bucket counter.

    Buckets rather than a histogram because the cache is a key-value store and
    a real histogram wants a real metrics backend. This is enough to see a
    regression, which is what it is for.
    """
    for bound in LATENCY_BUCKETS_MS:
        if elapsed_ms <= bound:
            incr(f'{name}.le_{bound}')
            return
    incr(f'{name}.over')


@contextmanager
def timed(name):
    """Time a block and record it. Yields a dict the caller can read the
    elapsed time back out of, for the log line."""
    started = time.perf_counter()
    holder = {'ms': 0.0}
    try:
        yield holder
    finally:
        holder['ms'] = (time.perf_counter() - started) * 1000.0
        observe_latency(name, holder['ms'])


def snapshot(names):
    """Read counters back. For the debug endpoint and for tests."""
    return {name: cache.get(_key(name), 0) for name in names}


def reset(names):
    """Clear counters. Tests only — there is no production caller."""
    for name in names:
        cache.delete(_key(name))


def log_ranking(viewer_id, *, stages, sources, distribution, latency_ms, cache_hit, page_size):
    """One line per ranked feed request.

    Ids and counts only. `stages` is the funnel — how many candidates entered
    and left each stage — which is the thing that makes "the feed is empty"
    diagnosable in one look instead of five.
    """
    logger.info(
        'recs.rank cache_hit=%s page=%s funnel=%s sources=%s served=%s latency_ms=%.1f',
        cache_hit,
        page_size,
        stages,
        sources,
        distribution,
        latency_ms,
    )
