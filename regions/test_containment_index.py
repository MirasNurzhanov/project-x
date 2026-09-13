"""H5: prepared rings answer exactly what the plain ring walk answers, only faster."""
import math
import random
from unittest.mock import patch

from django.test import SimpleTestCase

from . import geometry
from .models import Region
from .test_geometry import CHUKOTKA, FIJI, FIJI_SPLIT, KIRIBATI
from .tests import HOLE, SQUARE

EPS = geometry.EDGE_EPSILON_DEG


def jagged(rng, vertices, lat0, lon0, radius):
    ring = []
    for k in range(vertices - 1):
        angle = 2 * math.pi * k / (vertices - 1)
        r = radius * (0.3 + 0.7 * rng.random())
        ring.append([lon0 + r * math.cos(angle), lat0 + r * math.sin(angle)])
    ring.append(list(ring[0]))
    return ring


def probe_points(rng, rings, count, edges_per_ring=25):
    """Random points plus the awkward ones: vertices, edge midpoints, and points an epsilon either side."""
    min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
    pad = (max_lat - min_lat) * 0.05 + EPS
    points = [
        (rng.uniform(min_lat - pad, max_lat + pad), rng.uniform(min_lon - pad, max_lon + pad))
        for _ in range(count)
    ]
    for ring in rings:
        edges = list(zip(ring, ring[1:]))
        for (x1, y1), (x2, y2) in rng.sample(edges, min(len(edges), edges_per_ring)):
            mid_lat, mid_lon = (y1 + y2) / 2, (x1 + x2) / 2
            points += [(y1, x1), (mid_lat, mid_lon), (y1, rng.uniform(min_lon - pad, max_lon + pad))]
            for offset in (0.5 * EPS, EPS, 1.5 * EPS, 4 * EPS):
                points += [
                    (mid_lat + offset, mid_lon), (mid_lat - offset, mid_lon), (mid_lat, mid_lon + offset),
                    (y1, x1 - offset), (y1 + offset, x1),
                ]
    return points


class PreparedRingsMatchTheRingWalkTests(SimpleTestCase):
    def assert_same_answers(self, rings, points):
        prepared = geometry.prepare(rings)
        mismatches = [
            (lat, lon) for lat, lon in points
            if prepared.contain(lat, lon) != geometry.rings_contain(rings, lat, lon)
        ]
        self.assertEqual(mismatches, [])

    def test_jagged_rings_holes_and_islands(self):
        rng = random.Random(8130)
        for vertices in (3, 4, 7, 16, 64, 257, 600):
            outer = jagged(rng, vertices, 43.25, 76.9, 0.2)
            hole = jagged(rng, max(4, vertices // 4), 43.25, 76.9, 0.05)
            island = jagged(rng, max(4, vertices // 3), 43.7, 77.4, 0.1)
            for shape, rings in (('outer', [outer]), ('hole', [outer, hole]), ('islands', [outer, hole, island])):
                with self.subTest(vertices=vertices, shape=shape):
                    self.assert_same_answers(rings, probe_points(rng, rings, 300))

    def test_axis_aligned_square_with_a_hole_on_exact_boundaries(self):
        rings = [SQUARE, HOLE]
        grid = [i / 20 for i in range(-2, 23)]
        points = [(lat, lon) for lat in grid for lon in grid]
        self.assert_same_answers(rings, points + probe_points(random.Random(1), rings, 200))

    def test_degenerate_rings(self):
        for label, rings in (
            ('two points', [[[0.0, 0.0], [1.0, 1.0]]]),
            ('repeated vertices', [[[0.0, 0.0], [0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]),
            ('zero height', [[[0.0, 0.5], [1.0, 0.5], [2.0, 0.5], [0.0, 0.5]]]),
            ('one repeated point', [[[0.3, 0.3]] * 5]),
        ):
            with self.subTest(label):
                points = probe_points(random.Random(2), rings, 200) + [(0.5, 0.5), (0.3, 0.3), (0.5, 1.0)]
                self.assert_same_answers(rings, points)

    def test_antimeridian_regions_through_contains(self):
        rng = random.Random(3)
        for label, raw in (('fiji', [FIJI]), ('fiji split', FIJI_SPLIT), ('kiribati', [KIRIBATI]),
                           ('chukotka', [CHUKOTKA])):
            rings = geometry.unwrap_rings(raw)
            box = geometry.bounds(rings)
            prepared = geometry.prepare(rings)
            points = [(rng.uniform(box[0] - 1, box[2] + 1), rng.uniform(-180, 180)) for _ in range(500)]
            points += [(lat, geometry.wrap_lon(lon)) for lat, lon in probe_points(rng, rings, 0)]
            with self.subTest(label):
                mismatches = [
                    point for point in points
                    if geometry.contains(prepared, *box, *point) != geometry.contains(rings, *box, *point)
                ]
                self.assertEqual(mismatches, [])

    def test_tolerance_probes_match(self):
        rng = random.Random(4)
        rings = [jagged(rng, 300, 43.25, 76.9, 0.05)]
        box = geometry.bounds(rings)
        prepared = geometry.prepare(rings)
        points = probe_points(rng, rings, 400)
        for tolerance in (0, 1, 354.0):
            with self.subTest(tolerance=tolerance):
                mismatches = [
                    point for point in points
                    if geometry.contains_within(prepared, *box, *point, tolerance)
                    != geometry.contains_within(rings, *box, *point, tolerance)
                ]
                self.assertEqual(mismatches, [])


class RegionUsesPreparedRingsTests(SimpleTestCase):
    def region(self, rings):
        min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
        return Region(name='r', min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon, rings=rings)

    def test_the_index_is_built_once_per_region_instance(self):
        region = self.region([SQUARE])
        with patch('regions.geometry.prepare', wraps=geometry.prepare) as prepare:
            for _ in range(5):
                self.assertTrue(region.contains(0.5, 0.5))
                self.assertTrue(region.could_contain(0.5, 1.001, 354.0))
        prepare.assert_called_once()

    def test_replacing_the_rings_rebuilds_the_index(self):
        region = self.region([SQUARE])
        self.assertTrue(region.contains(0.5, 0.5))
        region.rings = [[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]]]
        self.assertFalse(region.contains(0.5, 0.5))
        self.assertTrue(region.contains(0.1, 0.1))
