"""Reproducible benchmark of the map poll's polygon-zone test (codebase review finding H5).

    python manage.py bench_containment --settings=core.test_settings

Times `Region.contains` (a friend's precise position) and `Region.could_contain` at the
non-friend grid tolerance, the way `proximity.views` calls them: a freshly loaded Region per
poll, then one call per candidate. Every answer is also compared with the plain ring-list
functions in `regions.geometry`, so a faster path that disagrees is reported, not hidden.
"""
import math
import random
import time

from django.core.management.base import BaseCommand

from regions import geometry
from regions.models import Region
from users.visibility import APPROX_GRID_M

#: The tolerance `users.visibility.position_tolerance_for` gives every non-friend candidate.
TOLERANCE_M = APPROX_GRID_M * 0.708


def jagged_ring(vertices, rng, centre=(43.25, 76.9), radius_deg=0.15):
    """A closed ring with deep concavities, so much of its bounding box lies outside it."""
    lat0, lon0 = centre
    lon_scale = math.cos(math.radians(lat0))
    points = []
    for k in range(vertices - 1):
        angle = 2 * math.pi * k / (vertices - 1)
        radius = radius_deg * (0.35 + 0.65 * rng.random())
        points.append([lon0 + radius * math.cos(angle) / lon_scale, lat0 + radius * math.sin(angle)])
    points.append(list(points[0]))
    return points


def new_region(rings):
    min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
    return Region(
        name='bench', min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        rings=rings, point_count=sum(len(ring) for ring in rings),
    )


def poll_seconds(rings, points, call, rounds):
    """Fastest of `rounds` simulated polls: a new Region, then `call` once per point."""
    best = float('inf')
    for _ in range(rounds):
        start = time.perf_counter()
        region = new_region(rings)
        for lat, lon in points:
            call(region, lat, lon)
        best = min(best, time.perf_counter() - start)
    return best


class Command(BaseCommand):
    help = 'Benchmark polygon-zone containment on the map poll path (review finding H5).'

    def add_arguments(self, parser):
        parser.add_argument('--sizes', default='1500,5000,20000',
                            help='Ring vertex counts to test, comma-separated.')
        parser.add_argument('--points', type=int, default=200, help='Candidates per simulated poll.')
        parser.add_argument('--rounds', type=int, default=3, help='Polls per measurement; the fastest is kept.')
        parser.add_argument('--seed', type=int, default=20260913)

    def handle(self, *args, sizes, points, rounds, seed, **options):
        self.stdout.write(
            f'candidates/poll={points} rounds={rounds} seed={seed} tolerance_m={TOLERANCE_M:.1f}')
        self.stdout.write(
            f'{"vertices":>8} {"mode":<22} {"ms/candidate":>13} {"ms x200":>9} {"ms x1000":>10} {"mismatches":>10}')
        for vertices in (int(size) for size in sizes.split(',')):
            rng = random.Random(seed + vertices)
            rings = geometry.unwrap_rings([jagged_ring(vertices, rng)])
            min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
            candidates = [
                (rng.uniform(min_lat, max_lat), rng.uniform(min_lon, max_lon)) for _ in range(points)
            ]
            box = (min_lat, min_lon, max_lat, max_lon)
            # Worst case for the tolerance path: all nine probes run and every one misses.
            misses = [
                (lat, lon) for lat, lon in candidates
                if not geometry.contains_within(rings, *box, lat, lon, TOLERANCE_M)
            ] or candidates
            scenarios = (
                ('contains', candidates,
                 lambda region, lat, lon: region.contains(lat, lon),
                 lambda lat, lon: geometry.contains(rings, *box, lat, lon)),
                ('could_contain', candidates,
                 lambda region, lat, lon: region.could_contain(lat, lon, TOLERANCE_M),
                 lambda lat, lon: geometry.contains_within(rings, *box, lat, lon, TOLERANCE_M)),
                ('could_contain (misses)', misses,
                 lambda region, lat, lon: region.could_contain(lat, lon, TOLERANCE_M),
                 lambda lat, lon: geometry.contains_within(rings, *box, lat, lon, TOLERANCE_M)),
            )
            for label, sample, call, reference in scenarios:
                seconds = poll_seconds(rings, sample, call, rounds)
                per_candidate_ms = seconds * 1000 / len(sample)
                region = new_region(rings)
                mismatches = sum(call(region, lat, lon) != reference(lat, lon) for lat, lon in sample)
                self.stdout.write(
                    f'{vertices:>8} {label:<22} {per_candidate_ms:>13.4f} '
                    f'{per_candidate_ms * 200:>9.1f} {per_candidate_ms * 1000:>10.1f} {mismatches:>10}'
                )
