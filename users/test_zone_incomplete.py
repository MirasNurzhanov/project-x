"""An incomplete zone blocks the edit that would fix it — and nothing else.

`zone_type='city'` with no `zone_region` is a real, ordinary state:

  * a new account is created in it (migration 0036 — CITY, following on, no
    boundary until the first location report resolves one), and
  * `zone_region` is SET_NULL, so evicting a cached Region returns an
    established user to it.

`_validate_zone` used to read both the type and the region through a fallback
to the stored row, then apply the "a polygon type needs a boundary" rule to
whatever came out. So the rule fired for anyone who merely *was* a polygon
zone, on every PATCH — and a user in this state could not change their radius,
switch their music zone on, set `zone_active`, or upload zone audio. Every one
of them came back 400 "Pick a place before switching to a city zone", about a
switch the request had not asked for.

The rule now fires only when the request itself names a shape. These tests hold
both halves of that: the unrelated edits go through, and every way of actually
storing a boundary-less polygon zone is still refused.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from regions.models import Region
from users.models import User
from users.test_zone_follow import FollowFixture


class IncompleteZoneFixture(FollowFixture):
    """Somebody whose zone is a city that has no boundary resolved."""

    def setUp(self):
        super().setUp()
        self.user = self.make_user(at=self.IN_ORBITA)
        User.objects.filter(pk=self.user.pk).update(
            zone_type=User.ZoneType.CITY, zone_region=None,
        )
        self.user.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch(self, **body):
        return self.client.patch(reverse('zone'), body, format='json')


class UnrelatedEditsAreNotBlocked(IncompleteZoneFixture):
    """Requirement: a PATCH changing an unrelated setting must not fail solely
    because the pre-existing configuration is incomplete."""

    def test_the_radius_can_still_be_changed(self):
        response = self.patch(zone_radius=800)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_radius, 800)

    def test_the_music_zone_can_still_be_switched_on(self):
        response = self.patch(music_zone_active=True, music_zone_radius=300)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.music_zone_active)
        self.assertEqual(self.user.music_zone_radius, 300)

    def test_music_alerts_can_still_be_switched_off(self):
        response = self.patch(music_zone_alerts=False)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.music_zone_alerts)

    def test_following_can_still_be_switched_off(self):
        """The switch that decides whether the boundary ever resolves itself
        must not be gated on the boundary having resolved."""
        response = self.patch(zone_follows_location=False)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertFalse(self.user.zone_follows_location)

    def test_zone_active_is_allowed_and_that_is_deliberate(self):
        """The explicit decision, pinned so it cannot be reversed by accident.

        `zone_active` says whether the zone is on, not what shape it is. A zone
        with no boundary is handled everywhere that reads one — discovery falls
        back to the radius (proximity.views._viewer_zone_contains) and the map
        draws a circle (`effective_zone_mode`) — so switching it on stores
        nothing invalid and shows nobody a zone that lies. Refusing would make
        the switch unreachable for every brand-new account, which is the state
        migration 0036 creates them in.
        """
        response = self.patch(zone_active=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.zone_active)

    def test_the_escape_hatch_still_works(self):
        """Switching to a circle is how somebody leaves this state by hand, and
        it names a shape — so it goes through the rule and is allowed by it."""
        response = self.patch(zone_type='circle')
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.CIRCLE)


class StoringAnInvalidZoneIsStillRefused(IncompleteZoneFixture):
    """Requirement: a request that explicitly switches to city/district, or
    otherwise modifies place-zone configuration, still needs a valid region."""

    def assertRefusedForNoBoundary(self, response):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('zone_region', response.data)
        self.assertEqual(response.data['code'][0], 'boundary_not_available')

    def test_switching_to_city_without_a_region_is_refused(self):
        self.assertRefusedForNoBoundary(self.patch(zone_type='city'))

    def test_switching_to_district_without_a_region_is_refused(self):
        self.assertRefusedForNoBoundary(self.patch(zone_type='district'))

    def test_naming_the_type_alongside_an_unrelated_field_is_still_refused(self):
        """The unrelated field does not buy the shape a pass."""
        self.assertRefusedForNoBoundary(self.patch(zone_type='city', zone_radius=800))

    def test_clearing_the_region_of_a_city_zone_is_refused(self):
        """An explicit null is a modification of place-zone configuration, not
        an absence of one — this is the case `'zone_region' in attrs` is for."""
        self.assertRefusedForNoBoundary(self.patch(zone_region=None))

    def test_nothing_is_written_when_the_shape_is_refused(self):
        self.patch(zone_type='city', zone_radius=800)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_radius, 500)


class ValidTransitionsStillWork(IncompleteZoneFixture):
    def test_a_city_with_its_boundary_is_accepted(self):
        response = self.patch(
            zone_type='city', zone_region=self.almaty.pk,
            latitude=self.IN_ORBITA[0], longitude=self.IN_ORBITA[1],
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.CITY)
        self.assertEqual(self.user.zone_region_id, self.almaty.pk)

    def test_a_district_with_its_boundary_is_accepted(self):
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=self.IN_ORBITA[0], longitude=self.IN_ORBITA[1],
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.DISTRICT)

    def test_a_kind_mismatch_is_still_refused(self):
        """Not weakened: a city boundary cannot be labelled a district zone."""
        response = self.patch(
            zone_type='district', zone_region=self.almaty.pk,
            latitude=self.IN_ORBITA[0], longitude=self.IN_ORBITA[1],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('zone_region', response.data)

    def test_reasserting_a_type_on_a_complete_zone_still_works(self):
        """The Place tab sends `zone_type` alone and relies on the stored region.
        The rule still runs for it — it just passes."""
        User.objects.filter(pk=self.user.pk).update(
            zone_type=User.ZoneType.CITY, zone_region=self.almaty,
        )
        # `force_authenticate` holds this instance, and the view validates
        # against it — so the in-memory copy has to catch up with the row.
        self.user.refresh_from_db()
        response = self.patch(zone_type='city')
        self.assertEqual(response.status_code, 200, response.data)


class EvictedRegionState(FollowFixture):
    """`zone_region` is SET_NULL: deleting a cached Region must not lock its
    users out of their own zone settings."""

    def test_an_established_user_survives_their_region_being_evicted(self):
        user = self.make_user(at=self.IN_ORBITA)
        User.objects.filter(pk=user.pk).update(
            zone_type=User.ZoneType.CITY, zone_region=self.almaty,
        )
        Region.objects.filter(pk=self.almaty.pk).delete()

        user.refresh_from_db()
        # The premise: SET_NULL really did leave the zone pointing at nothing.
        self.assertEqual(user.zone_type, User.ZoneType.CITY)
        self.assertIsNone(user.zone_region_id)

        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(reverse('zone'), {'zone_radius': 900}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        self.assertEqual(user.zone_radius, 900)


class FreshAccountState(FollowFixture):
    """The state migration 0036 creates every new account in, untouched."""

    def test_the_default_state_is_the_incomplete_one(self):
        user = User.objects.create_user(
            username='newcomer', email='newcomer@example.com', password='testpass-123',
        )
        self.assertEqual(user.zone_type, User.ZoneType.CITY)
        self.assertIsNone(user.zone_region_id)
        self.assertTrue(user.zone_follows_location)

    def test_a_brand_new_account_can_configure_its_zone(self):
        """No position reported, no boundary resolved, nothing touched."""
        user = User.objects.create_user(
            username='newcomer2', email='newcomer2@example.com', password='testpass-123',
        )
        client = APIClient()
        client.force_authenticate(user)

        for body in (
            {'zone_active': True},
            {'zone_radius': 750},
            {'music_zone_active': True},
            {'music_zone_alerts': False},
        ):
            response = client.patch(reverse('zone'), body, format='json')
            self.assertEqual(response.status_code, 200, (body, response.data))

        user.refresh_from_db()
        self.assertTrue(user.zone_active)
        self.assertEqual(user.zone_radius, 750)
        self.assertTrue(user.music_zone_active)
        self.assertFalse(user.music_zone_alerts)

    def test_a_brand_new_account_still_cannot_store_a_shapeless_city(self):
        user = User.objects.create_user(
            username='newcomer3', email='newcomer3@example.com', password='testpass-123',
        )
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(reverse('zone'), {'zone_type': 'city'}, format='json')
        self.assertEqual(response.status_code, 400)
