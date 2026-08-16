"""Viewer-governed discovery on /api/proximity/nearby/.

The viewer's own zone decides how far they see. `zone_contains(viewer.zone,
target.location)` — never the reverse, and never mutual. Someone who picks their
whole city sees everyone in it; the people they see keep whatever range they
chose, and mostly cannot see back.

Asymmetry is the property most easily broken by a well-meaning refactor toward
"is this pair within range of each other", so it is asserted directly and from
both sides rather than inferred from one direction working.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest, HiRequest
from interests.models import Interest
from regions.models import Region
from users.models import User
from users.testing import check_in


VIEWER_LAT, VIEWER_LON = 0.1, 0.1
#: ~11km east — far outside any circle the dial allows (5km max), and well
#: inside the 1°-square city below. One point that separates the two shapes.
FAR_LAT, FAR_LON = 0.1, 0.2

CITY_RINGS = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
#: A district in the city's south-west corner, ~22km on a side.
DISTRICT_RINGS = [[[0.0, 0.0], [0.2, 0.0], [0.2, 0.2], [0.0, 0.2], [0.0, 0.0]]]


def make_region(name, kind, rings, osm_id, bounds):
    min_lat, min_lon, max_lat, max_lon = bounds
    return Region.objects.create(
        osm_type='relation', osm_id=osm_id,
        name=name, display_name=f'{name}, Testland', kind=kind, rings=rings,
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=(min_lat + max_lat) / 2, centroid_lon=(min_lon + max_lon) / 2,
        point_count=5,
    )


class ViewerZoneDiscoveryTests(TestCase):
    def setUp(self):
        self.interest = Interest.objects.create(name='Test Zone Interest')
        self.city = make_region(
            'Metropolis', Region.Kind.CITY, CITY_RINGS, 3000, (0.0, 0.0, 1.0, 1.0),
        )
        self.district = make_region(
            'Old Town', Region.Kind.DISTRICT, DISTRICT_RINGS, 3001, (0.0, 0.0, 0.2, 0.2),
        )
        self.client = APIClient()

    def user(self, name, lat, lon, **extra):
        u = User.objects.create_user(
            username=name, email=f'{name}@example.com', password='testpass-123',
            latitude=str(lat), longitude=str(lon), **extra,
        )
        u.interests.set([self.interest])
        # Checked in, because this file is about how far a *zone* reaches. The
        # check-in gate is a separate question with its own suite; leaving it
        # off here would make every case below pass for the wrong reason.
        return check_in(u)

    def sees(self, viewer):
        """The ids `viewer` gets back from the discovery endpoint."""
        self.client.force_authenticate(viewer)
        response = self.client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()}

    # ── The five asymmetry cases ─────────────────────────────────────────────

    def test_a_city_viewer_sees_someone_10km_away_in_the_same_city(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        b = self.user('b', FAR_LAT, FAR_LON)
        self.assertIn(b.pk, self.sees(a))

    def test_a_500m_circle_viewer_cannot_see_the_city_viewer_10km_away(self):
        """The other half of the same pair. Visibility is not mutual."""
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        b = self.user('b', FAR_LAT, FAR_LON,
                      zone_type=User.ZoneType.CIRCLE, zone_radius=500)
        self.assertNotIn(a.pk, self.sees(b))
        # …and asserted together, so a change that makes it mutual fails here
        # rather than silently passing both single-direction tests.
        self.assertIn(b.pk, self.sees(a))

    def test_a_city_viewer_cannot_see_someone_just_outside_the_boundary(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        outside = self.user('outside', 1.05, 0.5)     # ~5km past the north edge
        self.assertNotIn(outside.pk, self.sees(a))

    def test_a_district_viewer_sees_inside_and_not_across_the_boundary(self):
        d = self.user('d', 0.1, 0.1,
                      zone_type=User.ZoneType.DISTRICT, zone_region=self.district)
        inside = self.user('inside', 0.15, 0.15)      # in the district
        across = self.user('across', 0.3, 0.3)        # in the city, not the district
        ids = self.sees(d)
        self.assertIn(inside.pk, ids)
        self.assertNotIn(across.pk, ids)

    def test_a_circle_viewer_uses_real_radius_distance(self):
        c = self.user('c', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CIRCLE, zone_radius=1000)
        near = self.user('near', 0.1045, 0.1)         # ~500m
        far = self.user('far', 0.115, 0.1)            # ~1.7km
        ids = self.sees(c)
        self.assertIn(near.pk, ids)
        self.assertNotIn(far.pk, ids)

    # ── The radius is the viewer's, not a constant ───────────────────────────

    def test_widening_the_dial_widens_what_you_see(self):
        """The old 500m assumption, gone: the same target flips on the viewer's
        own setting and nothing else."""
        c = self.user('c', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CIRCLE, zone_radius=500)
        target = self.user('target', 0.117, 0.1)      # ~1.9km
        self.assertNotIn(target.pk, self.sees(c))
        c.zone_radius = 3000
        c.save()
        self.assertIn(target.pk, self.sees(c))

    def test_the_default_account_still_sees_exactly_500m(self):
        """Nobody who has not touched the setting notices this change."""
        c = self.user('c', VIEWER_LAT, VIEWER_LON)   # circle / 500 by default
        near = self.user('near', 0.1027, 0.1)         # ~300m
        far = self.user('far', 0.1072, 0.1)           # ~800m
        ids = self.sees(c)
        self.assertIn(near.pk, ids)
        self.assertNotIn(far.pk, ids)

    def test_the_map_switch_does_not_gate_discovery(self):
        """`zone_active` draws the circle and does nothing else — the rule the
        rest of the codebase already holds to."""
        a = self.user('a', VIEWER_LAT, VIEWER_LON, zone_active=False,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        b = self.user('b', FAR_LAT, FAR_LON)
        self.assertIn(b.pk, self.sees(a))

    def test_an_evicted_boundary_falls_back_to_the_radius(self):
        """`zone_region` is SET_NULL. The viewer must get a usable map rather
        than an empty one or a 500."""
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=None, zone_radius=500)
        near = self.user('near', 0.1027, 0.1)         # ~300m
        far = self.user('far', FAR_LAT, FAR_LON)      # ~11km
        ids = self.sees(a)
        self.assertIn(near.pk, ids)
        self.assertNotIn(far.pk, ids)

    # ── What a wider zone must not bypass ────────────────────────────────────

    def test_a_city_zone_does_not_bypass_interest_matching(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        other = self.user('other', FAR_LAT, FAR_LON)
        other.interests.set([Interest.objects.create(name='Something Else')])
        self.assertNotIn(other.pk, self.sees(a))

    def test_a_city_zone_does_not_bypass_the_interest_opt_out(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        hidden = self.user('hidden', FAR_LAT, FAR_LON, discoverable_by_interest=False)
        self.assertNotIn(hidden.pk, self.sees(a))

    def test_a_city_zone_does_not_bypass_a_block(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        blocked = self.user('blocked', FAR_LAT, FAR_LON)
        BlockedUser.objects.create(blocker=blocked, blocked=a)
        self.assertNotIn(blocked.pk, self.sees(a))

    def test_a_city_zone_does_not_bypass_paused_sharing(self):
        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        paused = self.user('paused', FAR_LAT, FAR_LON,
                           location_sharing=User.LocationSharing.PAUSED)
        self.assertNotIn(paused.pk, self.sees(a))

    # ── Tiers that are not the stranger tier ─────────────────────────────────

    def test_a_friend_is_visible_at_any_distance_with_a_tiny_zone(self):
        c = self.user('c', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CIRCLE, zone_radius=50)
        far = self.user('far-friend', 40.0, 40.0)
        FriendRequest.objects.create(
            sender=far, receiver=c, status=FriendRequest.Status.ACCEPTED,
        )
        self.assertIn(far.pk, self.sees(c))

    def test_a_connection_keeps_its_1km_floor_under_a_tiny_zone(self):
        """Shrinking your zone is a statement about discovery. It should not
        drop somebody you have already exchanged a Hi with."""
        c = self.user('c', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CIRCLE, zone_radius=50)
        conn = self.user('conn', 0.1054, 0.1)         # ~600m: past 50m, under 1km
        HiRequest.objects.create(
            sender=c, receiver=conn, status=HiRequest.Status.ACCEPTED,
        )
        self.assertIn(conn.pk, self.sees(c))

    def test_a_city_zone_extends_the_connection_tier(self):
        c = self.user('c', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)
        conn = self.user('conn', FAR_LAT, FAR_LON)    # ~11km, past the 1km floor
        HiRequest.objects.create(
            sender=c, receiver=conn, status=HiRequest.Status.ACCEPTED,
        )
        self.assertIn(conn.pk, self.sees(c))

    # ── Cost ─────────────────────────────────────────────────────────────────

    def test_a_city_zone_does_not_cost_a_query_per_person(self):
        """/nearby/ is polled every 10s, and a city zone is the case where the
        candidate set stops being small."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        a = self.user('a', VIEWER_LAT, VIEWER_LON,
                      zone_type=User.ZoneType.CITY, zone_region=self.city)

        def count():
            self.client.force_authenticate(a)
            with CaptureQueriesContext(connection) as ctx:
                self.client.get('/api/proximity/nearby/')
            return len(ctx)

        self.user('in-city-1', 0.3, 0.3)
        baseline = count()
        for i in range(2, 6):
            self.user(f'in-city-{i}', 0.3, 0.3)
        self.assertEqual(len(self.sees(a)), 5)
        self.assertEqual(count(), baseline)
