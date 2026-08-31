"""The acceptance matrix for manual check-in, stated as one readable table.

Every cell here is covered somewhere else too — `test_map_visibility` and
`test_zone_reach` test the units, `test_compliance.BlockingThroughEveryPathTests`
tests the block paths. This file restates the three-way interaction *as the
requirement reads*, in one place, so the thing App Review will actually try can
be checked against the suite without assembling it out of four files.

    checked in    inside zone    →  visible
    checked in    outside zone   →  hidden   (geography still governs)
    checked out   inside zone    →  hidden   (check-in is required on top)
    checked out   outside zone   →  hidden

The third row is the load-bearing one: check-in is an *additional* term, so a
zone that would admit somebody does not admit them if they never checked in.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from interests.models import Interest
from users.models import User
from users.testing import share_location

NEARBY = '/api/proximity/nearby/'
PRIVACY = '/api/users/privacy/'


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


class CheckInZoneMatrixTests(TestCase):
    """Check-in and geography, every combination."""

    #: ~110m north of the viewer — comfortably inside a default 500m circle.
    INSIDE = (10.0010, 10.0000)
    #: ~1.1km north — comfortably outside it.
    OUTSIDE = (10.0100, 10.0000)

    def setUp(self):
        self.interest = Interest.objects.create(name='Matrix Bouldering')
        # Viewer on a default account: circle zone, 500m radius.
        self.viewer = make_user('matrix_viewer', latitude='10.0000', longitude='10.0000')
        self.viewer.interests.add(self.interest)
        share_location(self.viewer)

        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def subject(self, name, lat, lon, checked_in):
        """`checked_in=False` now means ghosted, not merely un-opted-in.

        There is no un-opted-in state left: sharing follows the OS permission,
        so the only way to hold a position and still be off the map is Ghost
        Mode. The parameter keeps its name because what it selects — visible or
        not — is unchanged.
        """
        user = make_user(name, latitude=str(lat), longitude=str(lon))
        user.interests.add(self.interest)
        share_location(user)
        if not checked_in:
            user.ghost_mode = True
            user.save(update_fields=['ghost_mode'])
        return user

    def visible_ids(self):
        response = self.client.get(NEARBY)
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()}

    def test_checked_in_and_inside_the_zone_is_visible(self):
        who = self.subject('in_in', *self.INSIDE, checked_in=True)
        self.assertIn(who.pk, self.visible_ids())

    def test_checked_in_but_outside_the_zone_is_hidden(self):
        """Checking in is not a bypass — geography still governs."""
        who = self.subject('in_out', *self.OUTSIDE, checked_in=True)
        self.assertNotIn(who.pk, self.visible_ids())

    def test_ghosted_but_inside_the_zone_is_hidden(self):
        """The cell that matters most.

        The geography would admit them, and they have a position on file. The
        only thing keeping them off the map is that they turned themselves off.
        """
        who = self.subject('out_in', *self.INSIDE, checked_in=False)
        self.assertNotIn(who.pk, self.visible_ids())

    def test_checked_out_and_outside_the_zone_is_hidden(self):
        """The corner that must not pass by some other route."""
        who = self.subject('out_out', *self.OUTSIDE, checked_in=False)
        self.assertNotIn(who.pk, self.visible_ids())

    def test_the_same_person_appears_only_after_they_leave_ghost_mode(self):
        """One subject, one position, one variable changed.

        The tests above use four people; this proves the difference is Ghost
        Mode itself and not something about how each was constructed.
        """
        who = self.subject('toggler', *self.INSIDE, checked_in=False)
        self.assertNotIn(who.pk, self.visible_ids())

        who.ghost_mode = False
        who.save(update_fields=['ghost_mode'])
        self.assertIn(who.pk, self.visible_ids())

    def test_ghosting_removes_them_from_the_very_next_request(self):
        """Immediately — not on a sweep, not at the next launch.

        The row keeps its position, because a ghost still needs it for their
        own clubs and region; what changes inside the PATCH is that every
        query stops returning them.
        """
        who = self.subject('leaver', *self.INSIDE, checked_in=True)
        self.assertIn(who.pk, self.visible_ids())

        their_client = APIClient()
        their_client.force_authenticate(who)
        response = their_client.patch(
            PRIVACY, {'ghost_mode': True}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)

        self.assertNotIn(who.pk, self.visible_ids())
        who.refresh_from_db()
        self.assertIsNotNone(who.latitude)

    def test_a_city_zone_widens_geography_and_nothing_else(self):
        """A wider zone must not become a way to see people who are ghosted."""
        from regions.models import Region

        region = Region.objects.create(
            osm_type='relation', osm_id=901,
            name='Matrixville', display_name='Matrixville',
            country_code='kz', kind=Region.Kind.CITY, admin_level=8,
            min_lat=9.0, min_lon=9.0, max_lat=11.0, max_lon=11.0,
            centroid_lat=10.0, centroid_lon=10.0,
            rings=[[[9.0, 9.0], [11.0, 9.0], [11.0, 11.0], [9.0, 11.0], [9.0, 9.0]]],
            point_count=5, source=Region.Source.OSM, verified=True,
        )
        self.viewer.zone_type = User.ZoneType.CITY
        self.viewer.zone_region = region
        self.viewer.save(update_fields=['zone_type', 'zone_region'])

        # Both far outside the old 500m circle, both well inside the city.
        checked_in = self.subject('city_in', 10.05, 10.05, checked_in=True)
        checked_out = self.subject('city_out', 10.06, 10.06, checked_in=False)

        visible = self.visible_ids()
        self.assertIn(checked_in.pk, visible, 'the city zone did not widen reach')
        self.assertNotIn(
            checked_out.pk, visible,
            'a city zone surfaced somebody who never checked in',
        )


class BlockingInteractionTests(TestCase):
    """A block is mutual invisibility, and it outranks every other rule.

    `test_compliance.BlockingThroughEveryPathTests` covers the map surfaces.
    This covers the other ways either side could still reach the other.
    """

    def setUp(self):
        self.a = make_user('block_a', latitude='10.0000', longitude='10.0000')
        self.b = make_user('block_b', latitude='10.0001', longitude='10.0001')
        interest = Interest.objects.create(name='Blocked Bouldering')
        for user in (self.a, self.b):
            user.interests.add(interest)
            share_location(user)

        self.client_a = APIClient()
        self.client_a.force_authenticate(self.a)
        self.client_b = APIClient()
        self.client_b.force_authenticate(self.b)

    def block(self):
        # 204: blocking answers with no body. Asserted rather than ignored so
        # a route that starts 404ing cannot leave every test below passing
        # because nobody was visible in the first place.
        response = self.client_a.post(f'/api/friends/{self.b.pk}/block/')
        self.assertEqual(response.status_code, 204, response.content)

    def nearby(self, client):
        return {row['id'] for row in client.get(NEARBY).json()}

    def test_the_setup_really_did_make_them_visible_to_each_other(self):
        """Control, so every assertion below is a change rather than a default."""
        self.assertIn(self.b.pk, self.nearby(self.client_a))
        self.assertIn(self.a.pk, self.nearby(self.client_b))

    def test_neither_sees_the_other_on_the_map(self):
        self.block()
        self.assertNotIn(self.b.pk, self.nearby(self.client_a))
        self.assertNotIn(self.a.pk, self.nearby(self.client_b))

    def test_neither_can_open_the_others_profile(self):
        """A 404, not a 403 — invisibility must not be detectable."""
        self.block()
        self.assertEqual(
            self.client_a.get(f'/api/users/profile/{self.b.pk}/').status_code, 404)
        self.assertEqual(
            self.client_b.get(f'/api/users/profile/{self.a.pk}/').status_code, 404)

    def test_neither_finds_the_other_by_username_search(self):
        self.block()
        for client, target in ((self.client_a, self.b), (self.client_b, self.a)):
            found = client.get('/api/users/search/', {'q': target.username}).json()
            self.assertNotIn(target.pk, {row['id'] for row in found})

    def test_blocking_does_not_hide_unrelated_people(self):
        """Negative control: a block is about one pair, not a general mute."""
        other = make_user('bystander', latitude='10.0001', longitude='10.0001')
        other.interests.set(self.a.interests.all())
        share_location(other)

        self.block()
        self.assertIn(other.pk, self.nearby(self.client_a))
