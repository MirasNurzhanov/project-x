"""The recommender's own endpoints.

The ranked feed itself is not here — it is `swifts.views.SwiftFeedView`, which
now calls `pipeline.page`. That placement is deliberate: the feed endpoint's
contract, its serializer and its location guarantees already exist and are
tested, and moving the URL would have broken every client for no gain. The
recommender changes what order the rows come back in, not what a row is.

What lives here is everything the recommender adds:

    POST /api/swifts/signals/     the client reporting what happened
    GET  /api/swifts/recs/debug/  the funnel, for whoever is debugging it
"""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import metrics
from .ingest import record
from .models import SwiftSignal
from .serializers import SignalBatchSerializer


class SwiftSignalView(APIView):
    """Report interactions.

    Batched, and rate-limited by scope rather than by the default user throttle:
    this endpoint is called far more often than any other in the app — once per
    card, in batches — and putting it on the ordinary user rate would either
    throttle normal browsing or force the ordinary rate up for everything.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'swift_signals'

    def post(self, request):
        serializer = SignalBatchSerializer(
            data=request.data, context={'viewer': request.user},
        )
        serializer.is_valid(raise_exception=True)

        resolved = serializer.validated_data['resolved']
        written = 0
        skipped = 0

        for entry in serializer.validated_data['signals']:
            swift_id = entry.get('swift_id')
            swift = resolved.get(swift_id) if swift_id is not None else None

            if swift_id is not None and swift is None:
                # Names a Swift this viewer cannot see, or one that is gone.
                skipped += 1
                continue
            if swift is None and entry['kind'] not in _CREATOR_LEVEL:
                # Every other kind is about a specific Swift and is meaningless
                # without one.
                skipped += 1
                continue

            record(
                request.user,
                entry['kind'],
                swift=swift,
                watch_ms=entry.get('watch_ms', 0),
                completion_pct=entry.get('completion_pct', 0),
                served_by=entry.get('served_by', ''),
                position=entry.get('position'),
            )
            written += 1
            metrics.incr(f'recs.signal.{entry["kind"]}')

        return Response(
            {'recorded': written, 'skipped': skipped},
            status=status.HTTP_202_ACCEPTED,
        )


#: Kinds that make sense without a Swift attached.
_CREATOR_LEVEL = frozenset({
    SwiftSignal.Kind.BLOCK_CREATOR,
    SwiftSignal.Kind.NOT_INTERESTED,
})


#: Counters the debug view reports. Named explicitly rather than scanned,
#: because the cache backend has no key enumeration and because an explicit
#: list is the documentation of what is measured.
REPORTED_COUNTERS = (
    'recs.session.hit',
    'recs.session.miss',
    *[f'recs.served.{s}' for s in (
        'friends', 'affinity', 'collaborative', 'similarity',
        'trending', 'new_creator', 'cold_start', 'unknown',
    )],
    *[f'recs.signal.{k}' for k, _ in SwiftSignal.Kind.choices],
    *[f'recs.page.le_{b}' for b in metrics.LATENCY_BUCKETS_MS],
    'recs.page.over',
)


class RecsDebugView(APIView):
    """The funnel for the requesting user, plus the global counters.

    Staff only. Not because the numbers are sensitive — they are counts — but
    because it runs a full ranking pass to produce the funnel, and an endpoint
    that does that on demand is an endpoint worth keeping behind a door.

    Returns no coordinates, no captions and no usernames: ids and counts, which
    is everything needed to answer why a feed looked the way it did.
    """

    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        from .pipeline import build_session

        ordered_ids, sources_by_id, stages, source_counts = build_session(request.user)

        distribution = {}
        for source in sources_by_id.values():
            key = source or 'unknown'
            distribution[key] = distribution.get(key, 0) + 1

        cache_hits = metrics.snapshot(['recs.session.hit', 'recs.session.miss'])
        total = cache_hits['recs.session.hit'] + cache_hits['recs.session.miss']

        return Response({
            'funnel': stages,
            'candidates_per_source': source_counts,
            'served_distribution': distribution,
            'session_size': len(ordered_ids),
            'cache_hit_rate': (
                round(cache_hits['recs.session.hit'] / total, 4) if total else None
            ),
            'counters': metrics.snapshot(list(REPORTED_COUNTERS)),
        })
