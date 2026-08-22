"""Benchmark the recommender against a synthetic corpus.

    python manage.py bench_recs --creators 200 --swifts 20 --viewers 20

Builds a corpus, gives the benchmark viewer a plausible history, then times the
pipeline stage by stage and reports queries as well as milliseconds — because
on a hosted Postgres the query count is usually the number that decides
latency, and a profile in wall-clock alone hides an N+1 behind a fast laptop.

Runs inside a transaction that is rolled back, so it is safe against a real
database. It is still pointed at a scratch one by preference.
"""
import random
import statistics
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from recs import candidates as sources
from recs import diversity, exposure, features, safety, scoring
from recs.ingest import record
from recs.models import SwiftSignal
from recs.params import load_params
from recs.pipeline import build_session, page


class Command(BaseCommand):
    help = 'Time the Swift recommender against a synthetic corpus.'

    def add_arguments(self, parser):
        parser.add_argument('--creators', type=int, default=200)
        parser.add_argument('--swifts', type=int, default=20,
                            help='Swifts per creator.')
        parser.add_argument('--viewers', type=int, default=20,
                            help='Other viewers, for the collaborative source.')
        parser.add_argument('--history', type=int, default=120,
                            help='Signals for the benchmark viewer.')
        parser.add_argument('--repeat', type=int, default=5)
        parser.add_argument('--seed', type=int, default=1)
        parser.add_argument('--keep', action='store_true',
                            help='Commit the corpus instead of rolling it back.')

    def handle(self, *args, **options):
        if options['keep']:
            self._run(options)
            return
        try:
            with transaction.atomic():
                self._run(options)
                raise _Rollback()
        except _Rollback:
            self.stdout.write(self.style.WARNING('\ncorpus rolled back'))

    # ── Corpus ────────────────────────────────────────────────────────────────
    def _build(self, options):
        from datetime import date, timedelta

        from friends.models import FriendRequest
        from swifts.models import SwiftVideo
        from users.models import User

        rng = random.Random(options['seed'])
        now = timezone.now()

        self.stdout.write('building corpus…')
        creators = User.objects.bulk_create([
            User(username=f'bench_c{i}', email=f'bench_c{i}@example.com',
                 date_of_birth=date(1995, 1, 1))
            for i in range(options['creators'])
        ])
        viewers = User.objects.bulk_create([
            User(username=f'bench_v{i}', email=f'bench_v{i}@example.com',
                 date_of_birth=date(1995, 1, 1))
            for i in range(options['viewers'])
        ])
        me = User.objects.create(
            username='bench_me', email='bench_me@example.com',
            date_of_birth=date(1995, 1, 1),
        )

        swifts = SwiftVideo.objects.bulk_create([
            SwiftVideo(
                author=creator,
                caption=f'bench {i}',
                video_url='https://cdn.example.com/v.mp4',
                thumbnail_url='https://cdn.example.com/v.jpg',
                duration_ms=rng.randint(5_000, 45_000),
                status=SwiftVideo.Status.READY,
                visibility=SwiftVideo.Visibility.PUBLIC,
                like_count=rng.randint(0, 500),
                comment_count=rng.randint(0, 60),
                repost_count=rng.randint(0, 30),
                view_count=rng.randint(10, 5_000),
            )
            for creator in creators
            for i in range(options['swifts'])
        ])
        # Spread them through the last fortnight. `auto_now_add` means this has
        # to be an UPDATE, and doing it per row would dominate the build.
        for offset in range(14):
            slice_ids = [s.id for s in swifts if s.id % 14 == offset]
            if slice_ids:
                SwiftVideo.objects.filter(id__in=slice_ids).update(
                    created_at=now - timedelta(days=offset, hours=rng.randint(0, 23)),
                )

        # A friend graph, so the in-network source has something to find.
        FriendRequest.objects.bulk_create([
            FriendRequest(sender=me, receiver=creator,
                          status=FriendRequest.Status.ACCEPTED)
            for creator in rng.sample(creators, min(30, len(creators)))
        ])

        self.stdout.write('seeding history…')
        pool = rng.sample(swifts, min(options['history'], len(swifts)))
        for swift in pool:
            record(
                me,
                rng.choice([
                    SwiftSignal.Kind.WATCH, SwiftSignal.Kind.WATCH,
                    SwiftSignal.Kind.LIKE, SwiftSignal.Kind.SKIP,
                ]),
                swift=swift,
                completion_pct=rng.randint(0, 100),
            )
        # Other viewers, so the collaborative hop has peers to traverse.
        for viewer in viewers:
            for swift in rng.sample(swifts, min(20, len(swifts))):
                record(viewer, SwiftSignal.Kind.LIKE, swift=swift)

        return me, len(swifts)

    # ── Timing ────────────────────────────────────────────────────────────────
    def _run(self, options):
        me, corpus_size = self._build(options)
        params = load_params()
        now = timezone.now()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\ncorpus: {corpus_size} swifts / {options["creators"]} creators'
        ))

        # Stage-by-stage, once, with query counts.
        self.stdout.write('\nstage breakdown (one cold pass)')
        with CaptureQueriesContext(connection) as q:
            t0 = time.perf_counter()
            pool, source_counts = sources.gather(me, params=params, now=now)
            t_gather = time.perf_counter() - t0
        self._line('generate', t_gather, len(q.captured_queries), len(pool))

        with CaptureQueriesContext(connection) as q:
            t0 = time.perf_counter()
            pool, dropped = safety.apply(me, pool)
            t_safety = time.perf_counter() - t0
        self._line('safety', t_safety, len(q.captured_queries), len(pool), f'-{dropped}')

        with CaptureQueriesContext(connection) as q:
            t0 = time.perf_counter()
            pool, dropped = exposure.apply(me, pool, params=params, now=now)
            t_exposure = time.perf_counter() - t0
        self._line('exposure', t_exposure, len(q.captured_queries), len(pool), f'-{dropped}')

        with CaptureQueriesContext(connection) as q:
            t0 = time.perf_counter()
            rows = sources.hydrate_rows(pool)
            pool = [c for c in pool if c.swift_id in rows]
            pool.sort(key=lambda c: -scoring.light_score(
                rows[c.swift_id], now=now, source_agreement=len(c.sources),
            ))
            pool = pool[:params.limits.light_ranked]
            t_light = time.perf_counter() - t0
        self._line('light', t_light, len(q.captured_queries), len(pool))

        with CaptureQueriesContext(connection) as q:
            t0 = time.perf_counter()
            feature_map = features.hydrate(me, pool, rows, params=params, now=now)
            t_hydrate = time.perf_counter() - t0
        self._line('hydrate', t_hydrate, len(q.captured_queries), len(feature_map))

        t0 = time.perf_counter()
        scored = scoring.heavy_score(feature_map, params=params)
        t_heavy = time.perf_counter() - t0
        self._line('heavy', t_heavy, 0, len(scored))

        t0 = time.perf_counter()
        arranged = diversity.arrange(
            scored, limit=params.limits.session_size, params=params, seed=1,
        )
        t_diversity = time.perf_counter() - t0
        self._line('diversity', t_diversity, 0, len(arranged))

        # End to end, repeated.
        self.stdout.write('\nend to end')
        cold = []
        for _ in range(options['repeat']):
            exposure.SwiftImpression.objects.filter(viewer=me).delete()
            with CaptureQueriesContext(connection) as q:
                t0 = time.perf_counter()
                build_session(me, params=params, now=now, seed=1)
                cold.append((time.perf_counter() - t0) * 1000)
            cold_queries = len(q.captured_queries)
        self._summary('cold session', cold, cold_queries)

        first = page(me, limit=params.limits.page_size)
        warm = []
        for _ in range(options['repeat']):
            with CaptureQueriesContext(connection) as q:
                t0 = time.perf_counter()
                page(me, cursor=first.cursor, limit=params.limits.page_size)
                warm.append((time.perf_counter() - t0) * 1000)
            warm_queries = len(q.captured_queries)
        self._summary('warm page', warm, warm_queries)

        self.stdout.write('\ncandidates per source')
        for source, count in sorted(source_counts.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f'  {source:<16}{count:>6}')

        self.stdout.write('\nserved distribution')
        for source, count in sorted(
            diversity.source_distribution(arranged).items(), key=lambda kv: -kv[1],
        ):
            self.stdout.write(f'  {source:<16}{count:>6}')

    def _line(self, name, seconds, queries, remaining, note=''):
        self.stdout.write(
            f'  {name:<12}{seconds * 1000:>8.1f}ms  {queries:>3}q  '
            f'{remaining:>5} candidates {note}'
        )

    def _summary(self, name, samples, queries):
        self.stdout.write(
            f'  {name:<14}median {statistics.median(samples):>7.1f}ms   '
            f'min {min(samples):>7.1f}ms   max {max(samples):>7.1f}ms   {queries}q'
        )


class _Rollback(Exception):
    """Unwinds the benchmark transaction. Never escapes `handle`."""
