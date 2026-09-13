"""H6: a traced outline has a bounded number of vertices, while geometry already stored stays readable."""
import math

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from users.models import User
from . import nominatim
from .models import Region
from .tests import make_region, make_user

CENTRE = (0.5, 0.5)


def circle_ring(points, radius=0.05, centre=CENTRE):
    lat0, lon0 = centre
    ring = [
        [lon0 + radius * math.cos(2 * math.pi * k / (points - 1)),
         lat0 + radius * math.sin(2 * math.pi * k / (points - 1))]
        for k in range(points - 1)
    ]
    ring.append(list(ring[0]))
    return ring


@override_settings(REGION_MAX_TRACED_RING_POINTS=40, REGION_MAX_TRACED_POINTS=60)
class TracedVertexLimitTests(TestCase):
    def setUp(self):
        user = make_user('tracer')
        user.latitude, user.longitude = CENTRE
        user.save()
        self.client = APIClient()
        # As a real request sees the user: reloaded from the database.
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def trace(self, rings):
        return self.client.post(
            '/api/regions/trace/', {'name': 'Courtyard', 'kind': 'district', 'rings': rings}, format='json')

    def test_a_ring_at_the_limit_is_accepted(self):
        response = self.trace([circle_ring(40)])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Region.objects.get().point_count, 40)

    def test_a_ring_over_the_limit_is_refused_before_storage(self):
        response = self.trace([circle_ring(41)])
        self.assertEqual(response.status_code, 400)
        self.assertIn('at most 40 points', str(response.json()['rings']))
        self.assertFalse(Region.objects.exists())

    def test_total_points_are_bounded_across_rings(self):
        hole = circle_ring(20, radius=0.01, centre=(0.53, 0.53))
        self.assertEqual(self.trace([circle_ring(40), hole]).status_code, 201)
        Region.objects.all().delete()

        response = self.trace([circle_ring(40), circle_ring(21, radius=0.01, centre=(0.53, 0.53))])
        self.assertEqual(response.status_code, 400)
        self.assertIn('Keep it to 60 in total', str(response.json()['rings']))
        self.assertFalse(Region.objects.exists())

    def test_too_many_rings_or_oversized_points_are_refused_without_parsing_them(self):
        for label, rings in (
            ('nine rings', [circle_ring(5, radius=0.001 * (k + 1)) for k in range(9)]),
            ('a point with 10,000 values', [[[0.5] * 10_000] + circle_ring(5)]),
            ('a ring of 100,000 points', [[[0.5, 0.5]] * 100_000]),
        ):
            with self.subTest(label):
                self.assertEqual(self.trace(rings).status_code, 400)
        self.assertFalse(Region.objects.exists())


class TracedVertexDefaultsTests(TestCase):
    def test_defaults_match_the_largest_boundary_osm_import_stores(self):
        self.assertEqual(settings.REGION_MAX_TRACED_POINTS, nominatim.MAX_POINTS)
        self.assertLessEqual(settings.REGION_MAX_TRACED_POINTS, 8 * settings.REGION_MAX_TRACED_RING_POINTS)

    @override_settings(REGION_MAX_TRACED_RING_POINTS=40, REGION_MAX_TRACED_POINTS=60)
    def test_geometry_stored_before_the_limit_stays_readable(self):
        owner = make_user('owner')
        client = APIClient()
        client.force_authenticate(owner)
        dense = [circle_ring(3000)]
        osm = make_region(osm_id=9001, rings=dense)
        traced = make_region(
            osm_type='', osm_id=None, name='Old trace', rings=dense,
            source=Region.Source.USER, verified=False, submitted_by=owner,
        )
        for region in (osm, traced):
            with self.subTest(region.name):
                response = client.get(f'/api/regions/{region.pk}/')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['rings'][0]), 3000)
                self.assertTrue(Region.objects.get(pk=region.pk).contains(*CENTRE))
