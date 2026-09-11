"""A zone that follows its owner from one district to the next.

The bug this exists to close: `zone_region` was a *pin*. Whichever district you
were standing in when you pressed "use my district" stayed your zone forever —
walk across town and it kept covering the neighbourhood you left, silently,
until you happened to reopen the zone screen. Meanwhile the same location report
was already recomputing which district you were in, for club visibility, and
throwing the answer away as far as the zone was concerned.

The geography is the same fixture shape `clubs/tests.py` uses, and for the same
reason: real polygons, really nested, resolved by the real pipeline with only
the network stubbed out. A mock of `snapshot_for` would prove that this module
calls it, which is not the thing in doubt — what is in doubt is whether the
tiers it hands back select the right boundary, and that is a fact about geometry.

    Almaty ........................... city     (0,0) → (1,1)
      Bostandyk ...................... district (0,0) → (0.5,0.5)
        Orbita ....................... district (0,0) → (0.2,0.2)
        Kazakhfilm ................... district (0.3,0.3) → (0.5,0.5)
      Almaly ......................... district (0.5,0.5) → (1,1)
    Astana ........................... city     (5,5) → (6,6)
    Karaganda ........................ city     (7,7) → (8,8)   ← no districts
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from regions import geometry, places
from regions.models import Region
from users import zones
from users.models import User
from users.testing import share_location


def square(min_lon, min_lat, max_lon, max_lat):
    return [[
        [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat],
        [min_lon, max_lat], [min_lon, min_lat],
    ]]


_osm_id = iter(range(500000, 600000))


def make_region(name, box, kind, admin_level):
    """A stored region, measured the way the real upsert measures one."""
    rings = geometry.unwrap_rings(square(*box))
    min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
    centroid_lat, centroid_lon = geometry.centroid(min_lat, min_lon, max_lat, max_lon)
    return Region.objects.create(
        osm_type=Region.OSMType.RELATION, osm_id=next(_osm_id),
        name=name, display_name=f'{name}, Testland', country_code='kz',
        kind=kind, admin_level=admin_level,
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=centroid_lat, centroid_lon=centroid_lon,
        rings=rings, point_count=sum(len(r) for r in rings),
    )


class FollowFixture(TestCase):
    """Two cities with districts, one without, and somebody who walks."""

    def setUp(self):
        super().setUp()
        # Every upstream stage pinned to "nothing out there", so every answer
        # comes from stored boundaries — which is also how the location path
        # behaves in production, where it runs cache-only.
        for target, value in (
            ('regions.nominatim.address_at', None),
            ('regions.nominatim._resolve_boundary', None),
            ('regions.overpass.boundaries_at', []),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.almaty = make_region('Almaty', (0, 0, 1, 1), Region.Kind.CITY, 4)
        self.bostandyk = make_region(
            'Bostandyk', (0, 0, 0.5, 0.5), Region.Kind.DISTRICT, 6)
        self.orbita = make_region('Orbita', (0, 0, 0.2, 0.2), Region.Kind.DISTRICT, 9)
        self.kazakhfilm = make_region(
            'Kazakhfilm', (0.3, 0.3, 0.5, 0.5), Region.Kind.DISTRICT, 9)
        self.almaly = make_region('Almaly', (0.5, 0.5, 1, 1), Region.Kind.DISTRICT, 6)

        self.astana = make_region('Astana', (5, 5, 6, 6), Region.Kind.CITY, 4)
        self.yesil = make_region('Yesil', (5, 5, 5.5, 5.5), Region.Kind.DISTRICT, 6)

        #: A city with no sub-area mapped at all — the "nowhere to move to" case.
        self.karaganda = make_region('Karaganda', (7, 7, 8, 8), Region.Kind.CITY, 4)

        # (lat, lon), the argument order everything server-side uses.
        self.IN_ORBITA = (0.1, 0.1)
        self.IN_KAZAKHFILM = (0.4, 0.4)
        self.IN_ALMALY = (0.7, 0.7)
        self.IN_YESIL = (5.2, 5.2)
        self.IN_KARAGANDA = (7.5, 7.5)

    # ── people ────────────────────────────────────────────────────────────────
    def make_user(self, name='walker', at=None, **fields):
        user = User.objects.create_user(
            username=name, email=f'{name}@example.com', password='testpass-123',
            **fields,
        )
        if at is not None:
            self.stand(user, at)
        return share_location(user)

    def stand(self, user, at):
        """Put a user at a coordinate the way a location report would, and
        refresh the place snapshot the follow reads."""
        user.latitude, user.longitude = at
        user.location_updated_at = timezone.now()
        user.location_accuracy_m = None
        user.save(update_fields=[
            'latitude', 'longitude', 'location_updated_at', 'location_accuracy_m',
        ])
        return places.snapshot_for(user, allow_upstream=False, force=True)

    def move(self, user, to):
        """Move, then run the follow exactly as the location endpoint does."""
        place = self.stand(user, to)
        zones.follow_location(user, place)
        user.refresh_from_db()
        return user

    def follower(self, at, zone_type=User.ZoneType.DISTRICT, region=None):
        """Somebody standing at `at` with a following zone already resolved."""
        user = self.make_user(at=at, zone_type=zone_type, zone_follows_location=True)
        place = places.snapshot_for(user, allow_upstream=False, force=True)
        zones.follow_location(user, place)
        user.refresh_from_db()
        if region is not None:
            self.assertEqual(user.zone_region_id, region.pk)
        return user


# ─── The behaviour asked for ──────────────────────────────────────────────────
class FollowingMovesTheZoneTests(FollowFixture):
    def test_the_zone_moves_to_the_district_the_user_walked_into(self):
        """The whole feature in one assertion."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KAZAKHFILM)
        self.assertEqual(user.zone_region_id, self.kazakhfilm.pk)

    def test_containment_follows_the_zone_rather_than_lagging_behind_it(self):
        """Re-pointing the column is only half of it — the containment service
        has to answer about the new boundary, or the zone has moved on paper
        and nowhere else."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KAZAKHFILM)

        self.assertTrue(zones.is_location_inside_zone(user, *self.IN_KAZAKHFILM))
        self.assertFalse(zones.is_location_inside_zone(user, *self.IN_ORBITA))

    def test_a_following_city_zone_moves_between_cities(self):
        user = self.follower(
            self.IN_ORBITA, zone_type=User.ZoneType.CITY, region=self.almaty)
        self.move(user, self.IN_YESIL)
        self.assertEqual(user.zone_region_id, self.astana.pk)

    def test_moving_within_the_same_district_leaves_the_zone_alone(self):
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, (0.15, 0.15))
        self.assertEqual(user.zone_region_id, self.orbita.pk)

    def test_moving_within_the_same_district_writes_nothing(self):
        """This runs on the endpoint every phone hits every thirty seconds. A
        save per report for somebody sitting still is the cost that makes a
        feature like this a bad idea."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        place = self.stand(user, (0.12, 0.12))
        with self.assertNumQueries(0):
            zones.follow_location(user, place)


class NotFollowingTests(FollowFixture):
    def test_a_pinned_zone_does_not_move(self):
        """The opt-in half. Somebody who chose a district by name keeps it,
        which is the distinction the flag encodes: *this place*, not *where I
        am*."""
        user = self.make_user(
            at=self.IN_ORBITA, zone_type=User.ZoneType.DISTRICT,
            zone_follows_location=False,
        )
        User.objects.filter(pk=user.pk).update(zone_region=self.orbita)
        user.refresh_from_db()

        self.move(user, self.IN_KAZAKHFILM)
        self.assertEqual(user.zone_region_id, self.orbita.pk)

    def test_following_is_on_by_default_so_a_new_zone_finds_its_city(self):
        """Flipped by migration 0036, together with the CITY default.

        The original guarantee behind the old `False` — that no zone anybody
        already has changes under them — is unaffected and is not this test's
        to make: a Django field default applies only to rows created after it,
        and 0036 is an AlterField, not a data migration. `User()` is an unsaved
        row, so this is the default itself and nothing else.
        """
        self.assertTrue(User().zone_follows_location)

    def test_a_circle_zone_is_untouched_even_with_following_on(self):
        """The flag persists across a type change the same way `zone_radius`
        does, so it has to be inert rather than merely unused."""
        user = self.make_user(
            at=self.IN_ORBITA, zone_type=User.ZoneType.CIRCLE,
            zone_radius=500, zone_follows_location=True,
        )
        self.move(user, self.IN_KAZAKHFILM)
        self.assertIsNone(user.zone_region_id)
        self.assertEqual(user.zone_type, User.ZoneType.CIRCLE)

    def test_a_user_with_no_position_is_left_alone(self):
        user = self.make_user(
            zone_type=User.ZoneType.DISTRICT, zone_follows_location=True)
        self.assertIsNone(zones.follow_location(user, None))


# ─── Nowhere to move to ───────────────────────────────────────────────────────
class NoDistrictMappedTests(FollowFixture):
    """OSM's coverage of sub-city areas is patchy, so this is ordinary rather
    than exceptional: a user walks into a city where nothing below it is
    mapped."""

    def test_the_zone_holds_its_last_district(self):
        """It does not empty itself. A zone that suddenly covers nobody is a
        worse answer than one that is briefly behind."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertEqual(user.zone_region_id, self.orbita.pk)

    def test_the_zone_does_not_silently_widen_to_the_city(self):
        """The other tempting fallback. Promoting a district zone to a whole
        city hands somebody a zone orders of magnitude larger than the one they
        asked for, without asking."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertNotEqual(user.zone_region_id, self.karaganda.pk)
        self.assertNotEqual(user.zone_region_id, self.almaty.pk)

    def test_it_is_flagged_stale(self):
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertTrue(user.zone_follow_stale)

    def test_the_flag_clears_when_a_district_resolves_again(self):
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertTrue(user.zone_follow_stale)

        self.move(user, self.IN_KAZAKHFILM)
        self.assertFalse(user.zone_follow_stale)
        self.assertEqual(user.zone_region_id, self.kazakhfilm.pk)

    def test_a_stale_zone_still_answers_containment(self):
        """Holding is only defensible if the zone keeps working."""
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertTrue(zones.is_location_inside_zone(user, *self.IN_ORBITA))

    def test_staleness_is_written_once_not_on_every_report(self):
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        place = self.stand(user, (7.6, 7.6))
        with self.assertNumQueries(0):
            zones.follow_location(user, place)


# ─── The kind rule ────────────────────────────────────────────────────────────
class TierKindTests(FollowFixture):
    """`UserPlace`'s tiers are positions in a containment chain; `Region.kind`
    is what a place actually is. The follow has to match on kind, because
    `ZoneUpdateSerializer` refuses a `zone_region` whose kind is not the zone
    type — a follow that ignored that would install a boundary the same user
    could not choose by hand."""

    def test_a_district_zone_never_picks_up_a_city(self):
        user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.move(user, self.IN_KARAGANDA)
        self.assertNotEqual(user.zone_region.kind, Region.Kind.CITY)

    def test_a_city_zone_never_picks_up_a_district(self):
        user = self.follower(
            self.IN_ORBITA, zone_type=User.ZoneType.CITY, region=self.almaty)
        self.move(user, self.IN_KAZAKHFILM)
        self.assertEqual(user.zone_region.kind, Region.Kind.CITY)

    def test_the_narrowest_matching_district_wins(self):
        """Same order the picker offers, so following and choosing by hand land
        on the same boundary."""
        user = self.follower(self.IN_ORBITA)
        # Orbita (0→0.2) sits inside Bostandyk (0→0.5); both are districts.
        self.assertEqual(user.zone_region_id, self.orbita.pk)

    def test_the_middle_tier_is_used_when_it_is_the_only_district(self):
        """Almaly is the widest sub-city area *and* a district, with nothing
        mapped below it. Reading `place.district` alone would find nothing and
        wrongly report the zone stale."""
        user = self.follower(self.IN_ALMALY)
        self.assertEqual(user.zone_region_id, self.almaly.pk)
        self.assertFalse(user.zone_follow_stale)


# ─── Through the API ──────────────────────────────────────────────────────────
class ZoneFollowApiTests(FollowFixture):
    def setUp(self):
        super().setUp()
        self.user = self.make_user(at=self.IN_ORBITA)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_flag_round_trips(self):
        response = self.client.patch(reverse('zone'), {
            'zone_type': 'district',
            'zone_region': self.orbita.pk,
            'zone_follows_location': True,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['zone_follows_location'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.zone_follows_location)

    def test_turning_it_on_repoints_the_zone_at_once(self):
        """Waiting for the next location report would leave the switch looking
        broken for a whole poll interval."""
        self.client.patch(reverse('zone'), {
            'zone_type': 'district', 'zone_region': self.orbita.pk,
        }, format='json')
        # Walk to another district without a report reaching the server.
        self.stand(self.user, self.IN_KAZAKHFILM)

        response = self.client.patch(
            reverse('zone'), {'zone_follows_location': True}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['zone_region'], self.kazakhfilm.pk)

    def test_staleness_is_reported_but_not_writable(self):
        """A client that could clear this could silence the only signal the
        zone screen has that its boundary is out of date.

        Set up as a district zone in a city with no districts mapped, which is
        the state that genuinely raises the flag — and, more to the point, one
        the server cannot resolve on its own. `ZoneView.patch` re-points a
        following zone on every request that does not name a region, and a
        re-point clears staleness by design, so a user standing anywhere the
        follow *can* answer would have the flag cleared out from under this
        assertion by the server rather than by the client. Then the test would
        be measuring the follow, not the serializer.

        It used to be written against `setUp`'s user, who is a CITY zone with
        no region — and it passed because that request was refused outright by
        the incomplete-zone bug (see users/test_zone_incomplete.py), not
        because the field was read-only. Now that the request goes through, the
        setup has to be one where the field is the only thing that could move.
        """
        # Its own user: `setUp` has already taken the default name.
        user = self.make_user(
            'stale-walker', at=self.IN_ORBITA,
            zone_type=User.ZoneType.DISTRICT, zone_follows_location=True,
        )
        self.move(user, self.IN_ORBITA)
        self.assertEqual(user.zone_region_id, self.orbita.pk)

        self.move(user, self.IN_KARAGANDA)
        self.assertTrue(user.zone_follow_stale, 'fixture should be stale')
        self.assertEqual(
            user.zone_region_id, self.orbita.pk,
            'a stale zone holds the boundary it left',
        )

        client = APIClient()
        client.force_authenticate(user)
        body = client.get(reverse('zone')).json()
        self.assertTrue(body['zone_follow_stale'])

        client.patch(reverse('zone'), {'zone_follow_stale': False}, format='json')
        user.refresh_from_db()
        self.assertTrue(user.zone_follow_stale)

    def test_a_following_zone_still_cannot_be_pointed_at_a_place_you_are_not_in(self):
        """The existing rule is untouched. Following changes *when* the zone
        moves, not who may set it where."""
        response = self.client.patch(reverse('zone'), {
            'zone_type': 'district',
            'zone_region': self.yesil.pk,
            'zone_follows_location': True,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_the_profile_reports_it_too(self):
        body = self.client.get(reverse('me')).json()
        self.assertIn('zone_follows_location', body)
        self.assertIn('zone_follow_stale', body)


class LocationEndpointTests(FollowFixture):
    """End to end: the zone moves on the back of an ordinary position report,
    which is the only path that moves it on its own."""

    def setUp(self):
        super().setUp()
        self.user = self.follower(self.IN_ORBITA, region=self.orbita)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_reporting_a_position_in_another_district_moves_the_zone(self):
        lat, lon = self.IN_KAZAKHFILM
        response = self.client.patch(
            reverse('update-location'),
            {'latitude': lat, 'longitude': lon}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, self.kazakhfilm.pk)

    def test_a_pinned_zone_is_untouched_by_the_same_report(self):
        self.user.zone_follows_location = False
        self.user.save(update_fields=['zone_follows_location'])

        lat, lon = self.IN_KAZAKHFILM
        self.client.patch(
            reverse('update-location'),
            {'latitude': lat, 'longitude': lon}, format='json',
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, self.orbita.pk)

    def test_a_failing_follow_never_fails_the_position_write(self):
        """Every other feature depends on the position landing. A zone that
        could not move must not take that down with it."""
        lat, lon = self.IN_KAZAKHFILM
        with patch('users.zones.follow_location', side_effect=RuntimeError('boom')):
            response = self.client.patch(
                reverse('update-location'),
                {'latitude': lat, 'longitude': lon}, format='json',
            )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(float(self.user.latitude), lat)
