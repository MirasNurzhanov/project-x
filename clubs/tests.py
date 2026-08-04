"""Location visibility for clubs.

The geography is a fixture rather than a mock: real polygons, really nested,
resolved by the real pipeline. What is stubbed is the network and nothing else
— `regions.nominatim` and `regions.overpass` are pinned to "the outside world
has nothing", so every answer here comes from stored boundaries, which is also
how the read path behaves in production.

The map, drawn once so the assertions can be read against it:

    Almaty ................................. city    (0,0) → (1,1)
      Bostandyk ............................ region  (0,0) → (0.5,0.5)
        Orbita ............................. district(0,0) → (0.2,0.2)
        Kazakhfilm ......................... district(0.3,0.3) → (0.5,0.5)
      Almaly ............................... region  (0.5,0.5) → (1,1)
        Zhetysu ............................ district(0.6,0.6) → (0.8,0.8)
    Astana ................................. city    (5,5) → (6,6)
      Yesil ................................ region  (5,5) → (5.5,5.5)

"region" and "district" here are the two sizes below a city, in that order.
Which one OSM would call a raion and which a microdistrict is not something
this feature asks — see regions.places for why the tiers are read off the
containment chain instead.
"""
import importlib
from io import StringIO
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from regions import geometry, places
from regions.models import Region, UserPlace

from . import visibility
from .models import Club, ClubMember

User = get_user_model()

legacy_migration = importlib.import_module(
    'clubs.migrations.0009_legacy_clubs_stay_visible'
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────
def square(min_lon, min_lat, max_lon, max_lat):
    return [[
        [min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat],
        [min_lon, max_lat], [min_lon, min_lat],
    ]]


_osm_id = iter(range(1000, 100000))


def make_region(name, box, kind, admin_level, country='kz', rings=None):
    """A stored region, measured the way the real upsert measures one."""
    rings = geometry.unwrap_rings(rings if rings is not None else square(*box))
    min_lat, min_lon, max_lat, max_lon = geometry.bounds(rings)
    centroid_lat, centroid_lon = geometry.centroid(min_lat, min_lon, max_lat, max_lon)
    return Region.objects.create(
        osm_type=Region.OSMType.RELATION, osm_id=next(_osm_id),
        name=name, display_name=f'{name}, Testland', country_code=country,
        kind=kind, admin_level=admin_level,
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon,
        centroid_lat=centroid_lat, centroid_lon=centroid_lon,
        rings=rings, point_count=sum(len(r) for r in rings),
    )


class GeoFixture(TestCase):
    """Two cities, five sub-areas, and people standing in them."""

    def setUp(self):
        super().setUp()
        # Every upstream stage of the resolver, pinned to "nothing out there".
        # Three separate seams: stubbing only one lets the others make real
        # requests from inside a test run.
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
        self.orbita = make_region(
            'Orbita', (0, 0, 0.2, 0.2), Region.Kind.DISTRICT, 9)
        self.kazakhfilm = make_region(
            'Kazakhfilm', (0.3, 0.3, 0.5, 0.5), Region.Kind.DISTRICT, 9)
        self.almaly = make_region(
            'Almaly', (0.5, 0.5, 1, 1), Region.Kind.DISTRICT, 6)
        self.zhetysu = make_region(
            'Zhetysu', (0.6, 0.6, 0.8, 0.8), Region.Kind.DISTRICT, 9)

        self.astana = make_region('Astana', (5, 5, 6, 6), Region.Kind.CITY, 4)
        self.yesil = make_region(
            'Yesil', (5, 5, 5.5, 5.5), Region.Kind.DISTRICT, 6)

        # (lat, lon) — the argument order everything server-side uses.
        self.IN_ORBITA = (0.1, 0.1)
        self.IN_KAZAKHFILM = (0.4, 0.4)
        self.IN_ZHETYSU = (0.7, 0.7)
        self.IN_ASTANA = (5.2, 5.2)

    # ── people ────────────────────────────────────────────────────────────────
    def make_user(self, name, at=None):
        user = User.objects.create_user(
            username=name, email=f'{name}@example.com', password='pw',
        )
        if at is not None:
            self.stand(user, at)
        return user

    def stand(self, user, at, accuracy=None):
        """Put a user at a coordinate the way a location report would."""
        user.latitude, user.longitude = at
        user.location_updated_at = timezone.now()
        user.location_accuracy_m = accuracy
        user.save(update_fields=[
            'latitude', 'longitude', 'location_updated_at', 'location_accuracy_m',
        ])
        places.snapshot_for(user, allow_upstream=False, force=True)
        return user

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def place_of(self, user):
        return visibility.place_for(user)

    # ── clubs ─────────────────────────────────────────────────────────────────
    def make_club(self, name, level, place=None, creator=None, **overrides):
        creator = creator or self.make_user(f'owner-{name}', at=self.IN_ORBITA)
        fields = (
            visibility.scope_fields(level, place) if place is not None
            else {'visibility_level': level}
        )
        fields.update(overrides)
        club = Club.objects.create(name=name, creator=creator, **fields)
        ClubMember.objects.create(user=creator, club=club, role=ClubMember.Role.OWNER)
        return club


# ─── 1-8: the rule itself ─────────────────────────────────────────────────────
class CanSeeClubTests(GeoFixture):
    """The eight combinations of club level and viewer position.

    Asserted through `can_see` *and* through the queryset filter, because the
    two are separate implementations of one rule and a feature enforced in SQL
    is only as good as the SQL.
    """

    def setUp(self):
        super().setUp()
        self.orbita_user = self.make_user('orbita', at=self.IN_ORBITA)
        self.kazakhfilm_user = self.make_user('kfilm', at=self.IN_KAZAKHFILM)
        self.zhetysu_user = self.make_user('zhetysu', at=self.IN_ZHETYSU)
        self.astana_user = self.make_user('astana', at=self.IN_ASTANA)

        home = self.place_of(self.orbita_user)
        self.district_club = self.make_club('Orbita Runners', 'district', home)
        self.region_club = self.make_club('Bostandyk Chess', 'region', home)
        self.city_club = self.make_club('Almaty Film', 'city', home)

    def assertVisible(self, club, user, expected):
        place = self.place_of(user)
        self.assertIs(visibility.can_see(club, place), expected)
        listed = visibility.visible_to(Club.objects.all(), place)
        self.assertIs(listed.filter(pk=club.pk).exists(), expected)

    # 1
    def test_district_club_seen_from_the_same_district(self):
        self.assertVisible(self.district_club, self.orbita_user, True)

    # 2
    def test_district_club_hidden_from_another_district_in_the_same_region(self):
        place = self.place_of(self.kazakhfilm_user)
        self.assertEqual(place.region_id, self.bostandyk.pk)
        self.assertEqual(place.district_id, self.kazakhfilm.pk)
        self.assertVisible(self.district_club, self.kazakhfilm_user, False)

    # 3
    def test_district_club_hidden_from_another_region_in_the_same_city(self):
        self.assertVisible(self.district_club, self.zhetysu_user, False)

    # 4
    def test_district_club_hidden_from_another_city(self):
        self.assertVisible(self.district_club, self.astana_user, False)

    # 5
    def test_region_club_seen_from_the_same_region(self):
        # Different district, same region — the case that separates the two
        # tiers from each other rather than from the city.
        self.assertVisible(self.region_club, self.kazakhfilm_user, True)
        self.assertVisible(self.region_club, self.orbita_user, True)

    # 6
    def test_region_club_hidden_from_another_region(self):
        self.assertVisible(self.region_club, self.zhetysu_user, False)

    # 7
    def test_city_club_seen_from_anywhere_in_the_city(self):
        for user in (self.orbita_user, self.kazakhfilm_user, self.zhetysu_user):
            self.assertVisible(self.city_club, user, True)

    # 8
    def test_city_club_hidden_from_another_city(self):
        self.assertVisible(self.city_club, self.astana_user, False)

    def test_city_is_the_ceiling(self):
        """No level, however wide, reaches out of the city it was made in."""
        place = self.place_of(self.astana_user)
        for club in (self.district_club, self.region_club, self.city_club):
            self.assertFalse(visibility.can_see(club, place))

    def test_object_rule_and_queryset_filter_agree(self):
        """The parity `visibility.py` promises, over every pairing."""
        clubs = list(Club.objects.all())
        for user in (self.orbita_user, self.kazakhfilm_user,
                     self.zhetysu_user, self.astana_user):
            place = self.place_of(user)
            filtered = set(
                visibility.visible_to(Club.objects.all(), place)
                .values_list('pk', flat=True)
            )
            for club in clubs:
                self.assertIs(
                    visibility.can_see(club, place), club.pk in filtered,
                    f'{club.name} vs {user.username}',
                )


# ─── 9-10: missing data, on either side ───────────────────────────────────────
class UnresolvedLocationTests(GeoFixture):
    # 9
    def test_user_without_a_location_sees_no_restricted_clubs(self):
        home = self.place_of(self.make_user('local', at=self.IN_ORBITA))
        city_club = self.make_club('Almaty Film', 'city', home)
        legacy = self.make_club('Old Club', Club.Visibility.GLOBAL)

        nomad = self.make_user('nomad')  # never reported a position
        place = self.place_of(nomad)

        self.assertFalse(place.resolved)
        self.assertEqual(place.reason, visibility.Unresolved.NO_LOCATION)
        self.assertFalse(visibility.can_see(city_club, place))
        # The legacy set is the *only* thing an unplaceable user sees.
        self.assertEqual(
            list(visibility.visible_to(Club.objects.all(), place)), [legacy],
        )

    def test_unresolved_user_is_told_why(self):
        nomad = self.make_user('nomad')
        response = self.client_for(nomad).get('/api/clubs/my-location/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['resolved'])
        self.assertEqual(response.data['location_status'], 'no_location')
        self.assertTrue(response.data['detail'])
        self.assertEqual(response.data['available_levels'], [])

    def test_stale_position_does_not_admit_anyone(self):
        from datetime import timedelta

        user = self.make_user('gone', at=self.IN_ORBITA)
        long_ago = timezone.now() - timedelta(days=2)
        User.objects.filter(pk=user.pk).update(
            location_updated_at=long_ago, updated_at=long_ago,
        )
        place = self.place_of(User.objects.get(pk=user.pk))
        self.assertFalse(place.resolved)
        self.assertEqual(place.reason, visibility.Unresolved.STALE)

    def test_a_fix_too_coarse_to_place_anyone_is_refused(self):
        user = self.make_user('foggy', at=self.IN_ORBITA)
        user.location_accuracy_m = 25_000
        user.save(update_fields=['location_accuracy_m'])
        place = self.place_of(user)
        self.assertFalse(place.resolved)
        self.assertEqual(place.reason, visibility.Unresolved.INACCURATE)

    def test_city_that_is_not_mapped_yet_reports_unmapped(self):
        user = self.make_user('pioneer', at=(40.0, 40.0))  # nothing mapped there
        place = self.place_of(user)
        self.assertFalse(place.resolved)
        self.assertEqual(place.reason, visibility.Unresolved.UNMAPPED)

    # 10
    def test_club_missing_its_own_tier_is_visible_to_nobody(self):
        """A district-level club with no district recorded fails closed.

        The direction matters: the alternative reading — "no district means no
        restriction" — turns a data gap into a leak.
        """
        home = self.place_of(self.make_user('local', at=self.IN_ORBITA))
        broken = self.make_club(
            'Ghost', 'district', home,
            visibility_district=None, visibility_district_osm='',
            visibility_district_name='',
        )
        no_region = self.make_club(
            'Ghost Two', 'region', home,
            visibility_region=None, visibility_region_osm='',
            visibility_region_name='',
        )
        no_city = self.make_club(
            'Ghost Three', 'city', home,
            visibility_city=None, visibility_city_osm='',
            visibility_city_name='',
        )

        for index, at in enumerate((self.IN_ORBITA, self.IN_KAZAKHFILM, self.IN_ASTANA)):
            place = self.place_of(self.make_user(f'viewer{index}', at=at))
            for club in (broken, no_region, no_city):
                self.assertFalse(visibility.can_see(club, place), club.name)
                self.assertFalse(
                    visibility.visible_to(Club.objects.all(), place)
                    .filter(pk=club.pk).exists(),
                    club.name,
                )

    def test_evicted_and_refetched_region_is_still_the_same_place(self):
        """A club outlives the cache row it was scoped to.

        Regions are a shared cache: evict Orbita and the club's foreign key
        goes null, then the next person to stand there re-fetches the identical
        OSM boundary under a *new* primary key. Matching on the key alone would
        have silently unscoped the club — visible to nobody, for as long as it
        existed. The OSM reference recognises it as what it is.
        """
        local = self.make_user('local', at=self.IN_ORBITA)
        club = self.make_club('Orbita Runners', 'district', self.place_of(local))
        osm_ref = self.orbita.osm_ref
        self.assertTrue(osm_ref)

        self.orbita.delete()  # SET_NULL on both sides
        club.refresh_from_db()
        self.assertIsNone(club.visibility_district_id)
        self.assertEqual(club.visibility_district_osm, osm_ref)

        # Re-fetched: same boundary, same OSM id, brand new row.
        refetched = make_region(
            'Orbita', (0, 0, 0.2, 0.2), Region.Kind.DISTRICT, 9)
        Region.objects.filter(pk=refetched.pk).update(
            osm_id=int(osm_ref[1:]), osm_type=Region.OSMType.RELATION,
        )
        refetched.refresh_from_db()
        self.assertNotEqual(refetched.pk, club.visibility_district_id)

        self.stand(local, self.IN_ORBITA)
        place = self.place_of(local)
        self.assertTrue(visibility.can_see(club, place))
        self.assertTrue(
            visibility.visible_to(Club.objects.all(), place)
            .filter(pk=club.pk).exists()
        )

        # And it is still not visible from the next district over.
        outsider = self.place_of(self.make_user('kfilm', at=self.IN_KAZAKHFILM))
        self.assertFalse(visibility.can_see(club, outsider))

    def test_a_boundary_that_is_simply_gone_fails_closed(self):
        """No polygon, no containment, no visibility — not a name match.

        A hand-traced boundary has no OSM identity, so deleting it takes the
        only two things that could identify it. The club keeps its *name*, and
        matching on that would place people inside a shape nobody holds on the
        strength of a different shape sharing a word. It stays hidden instead.
        """
        traced = make_region('Sunny Yard', (0.05, 0.05, 0.09, 0.09),
                             Region.Kind.DISTRICT, None)
        Region.objects.filter(pk=traced.pk).update(
            osm_type='', osm_id=None, source=Region.Source.USER, verified=True,
        )
        local = self.make_user('local', at=(0.07, 0.07))
        place = self.place_of(local)
        self.assertEqual(place.district_name, 'Sunny Yard')

        club = self.make_club('Yard Chess', 'district', place)
        self.assertTrue(visibility.can_see(club, place))

        Region.objects.filter(pk=traced.pk).delete()
        club.refresh_from_db()
        self.assertIsNone(club.visibility_district_id)
        self.assertEqual(club.visibility_district_osm, '')
        self.assertEqual(club.visibility_district_name, 'Sunny Yard')

        # An identically named area elsewhere must not let anyone in either.
        make_region('Sunny Yard', (0.35, 0.35, 0.39, 0.39), Region.Kind.DISTRICT, 10)
        impostor = self.place_of(self.make_user('elsewhere', at=(0.37, 0.37)))
        self.assertEqual(impostor.district_name, 'Sunny Yard')
        self.assertFalse(visibility.can_see(club, impostor))
        self.assertFalse(
            visibility.visible_to(Club.objects.all(), impostor)
            .filter(pk=club.pk).exists()
        )

        self.stand(local, (0.07, 0.07))
        self.assertFalse(visibility.can_see(club, self.place_of(local)))


# ─── 11: the API, asked directly ──────────────────────────────────────────────
class ApiEnforcementTests(GeoFixture):
    def setUp(self):
        super().setUp()
        self.local = self.make_user('local', at=self.IN_ORBITA)
        self.outsider = self.make_user('outsider', at=self.IN_ASTANA)
        home = self.place_of(self.local)
        self.district_club = self.make_club(
            'Orbita Runners', 'district', home, creator=self.local)
        self.city_club = self.make_club('Almaty Film', 'city', home)

    # 11
    def test_direct_lookup_from_outside_is_not_found(self):
        response = self.client_for(self.outsider).get(
            f'/api/clubs/{self.district_club.pk}/')
        self.assertEqual(response.status_code, 404)
        # No hint that the club exists — the same body a deleted club gives.
        self.assertEqual(response.data['detail'], 'Not found.')
        self.assertNotIn('location_status', response.data)

    def test_direct_lookup_without_a_location_says_what_to_do(self):
        nomad = self.make_user('nomad')
        response = self.client_for(nomad).get(f'/api/clubs/{self.city_club.pk}/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['location_status'], 'no_location')

    def test_members_roster_is_gated_the_same_way(self):
        response = self.client_for(self.outsider).get(
            f'/api/clubs/{self.district_club.pk}/members/')
        self.assertEqual(response.status_code, 404)

    def test_joining_from_outside_is_refused(self):
        response = self.client_for(self.outsider).post(
            f'/api/clubs/{self.district_club.pk}/join/')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            ClubMember.objects.filter(
                club=self.district_club, user=self.outsider).exists()
        )

    def test_messages_and_events_do_not_confirm_a_hidden_club_exists(self):
        """Members-only endpoints answer 404, not 403, from outside the area.

        A 403 saying "members only" is a confirmation that the club is there,
        which is the thing the whole feature is meant not to say.
        """
        client = self.client_for(self.outsider)
        for path in ('messages/', 'events/'):
            response = client.get(f'/api/clubs/{self.district_club.pk}/{path}')
            self.assertEqual(response.status_code, 404, path)

        posted = client.post(
            f'/api/clubs/{self.district_club.pk}/messages/', {'content': 'hi'},
            format='json',
        )
        self.assertEqual(posted.status_code, 404)

    def test_a_non_member_inside_the_area_is_told_members_only(self):
        """The other half of that ordering: visible, just not joined."""
        neighbour = self.make_user('neighbour', at=self.IN_ORBITA)
        response = self.client_for(neighbour).get(
            f'/api/clubs/{self.district_club.pk}/messages/')
        self.assertEqual(response.status_code, 403)

    def test_an_event_inherits_its_club_gate(self):
        from datetime import timedelta

        from .models import ClubEvent

        event = ClubEvent.objects.create(
            club=self.district_club, created_by=self.local, title='Sunday run',
            event_datetime=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(
            self.client_for(self.outsider).get(f'/api/clubs/events/{event.pk}/').status_code,
            404,
        )
        self.assertEqual(
            self.client_for(self.local).get(f'/api/clubs/events/{event.pk}/').status_code,
            200,
        )

    def test_discovery_list_is_filtered(self):
        names = {
            c['name'] for c in self.client_for(self.outsider).get('/api/clubs/').data
        }
        self.assertNotIn('Orbita Runners', names)
        self.assertNotIn('Almaty Film', names)

    def test_search_cannot_reach_across_the_filter(self):
        response = self.client_for(self.outsider).get('/api/clubs/?search=Orbita')
        self.assertEqual(list(response.data), [])

    def test_local_sees_both_and_can_join(self):
        newcomer = self.make_user('newcomer', at=self.IN_ORBITA)
        names = {c['name'] for c in self.client_for(newcomer).get('/api/clubs/').data}
        self.assertEqual(names, {'Orbita Runners', 'Almaty Film'})

        response = self.client_for(newcomer).post(
            f'/api/clubs/{self.district_club.pk}/join/')
        self.assertEqual(response.status_code, 201)

    def test_profile_does_not_leak_a_hidden_club(self):
        """A stranger's profile is a club list like any other."""
        response = self.client_for(self.outsider).get(
            f'/api/users/{self.local.pk}/profile/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.get('clubs', []), [])

        insider = self.make_user('insider', at=self.IN_ORBITA)
        response = self.client_for(insider).get(f'/api/users/{self.local.pk}/profile/')
        self.assertEqual(
            [c['name'] for c in response.data['clubs']], ['Orbita Runners'],
        )

    def test_club_payload_names_where_it_reaches(self):
        response = self.client_for(self.local).get(
            f'/api/clubs/{self.district_club.pk}/')
        self.assertEqual(response.data['visibility_level'], 'district')
        self.assertEqual(response.data['visibility']['place_name'], 'Orbita')
        self.assertEqual(response.data['visibility']['city']['name'], 'Almaty')
        self.assertEqual(response.data['visibility']['district']['id'], self.orbita.pk)


# ─── Creating a club ──────────────────────────────────────────────────────────
class CreateClubTests(GeoFixture):
    def setUp(self):
        super().setUp()
        self.creator = self.make_user('creator', at=self.IN_ORBITA)

    def create(self, **payload):
        body = {'name': 'New Club'}
        body.update(payload)
        return self.client_for(self.creator).post('/api/clubs/', body, format='json')

    def test_scope_comes_from_the_server_not_the_request(self):
        response = self.create(
            visibility_level='district',
            # All of this is ignored; the club lands where the creator is.
            visibility_city=self.astana.pk,
            visibility_district=self.yesil.pk,
            visibility_city_name='Astana',
        )
        self.assertEqual(response.status_code, 201)

        club = Club.objects.get(name='New Club')
        self.assertEqual(club.visibility_level, 'district')
        self.assertEqual(club.visibility_city_id, self.almaty.pk)
        self.assertEqual(club.visibility_region_id, self.bostandyk.pk)
        self.assertEqual(club.visibility_district_id, self.orbita.pk)
        self.assertEqual(club.visibility_country_code, 'kz')

    def test_every_tier_is_recorded_whatever_the_level(self):
        self.create(visibility_level='city')
        club = Club.objects.get(name='New Club')
        self.assertEqual(club.visibility_level, 'city')
        self.assertEqual(club.visibility_district_id, self.orbita.pk)

    def test_level_is_required(self):
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertIn('visibility_level', response.data)
        self.assertFalse(Club.objects.filter(name='New Club').exists())

    def test_level_must_be_one_of_the_three(self):
        for level in ('global', 'country', 'world', ''):
            response = self.create(visibility_level=level)
            self.assertEqual(response.status_code, 400, level)
        self.assertFalse(Club.objects.filter(name='New Club').exists())

    def test_options_offered_are_the_tiers_we_actually_hold(self):
        response = self.client_for(self.creator).get('/api/clubs/my-location/')
        self.assertEqual(
            response.data['available_levels'], ['district', 'region', 'city'],
        )
        self.assertEqual(response.data['city']['name'], 'Almaty')
        self.assertEqual(response.data['region']['name'], 'Bostandyk')
        self.assertEqual(response.data['district']['name'], 'Orbita')

    def test_a_level_with_no_boundary_is_not_offered_or_accepted(self):
        """Astana has a region mapped but no district inside it."""
        astana_user = self.make_user('astana', at=self.IN_ASTANA)
        response = self.client_for(astana_user).get('/api/clubs/my-location/')
        self.assertEqual(response.data['available_levels'], ['region', 'city'])
        self.assertIsNone(response.data['district'])

        response = self.client_for(astana_user).post(
            '/api/clubs/', {'name': 'Impossible', 'visibility_level': 'district'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['available_levels'], ['region', 'city'])
        self.assertFalse(Club.objects.filter(name='Impossible').exists())

    def test_cannot_create_without_a_location(self):
        nomad = self.make_user('nomad')
        response = self.client_for(nomad).post(
            '/api/clubs/', {'name': 'Nowhere', 'visibility_level': 'city'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['location_status'], 'no_location')
        self.assertFalse(Club.objects.filter(name='Nowhere').exists())

    def test_moving_between_picker_and_submit_is_a_conflict(self):
        """The screen showed Kazakhfilm; the creator is in Orbita by the time
        they press create. Better to ask again than to scope it wrongly."""
        response = self.create(
            visibility_level='district', visibility_place_id=self.kazakhfilm.pk,
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.data['location_changed'])
        self.assertFalse(Club.objects.filter(name='New Club').exists())

    def test_matching_place_id_is_accepted(self):
        response = self.create(
            visibility_level='district', visibility_place_id=self.orbita.pk,
        )
        self.assertEqual(response.status_code, 201)

    def test_creator_is_owner_and_the_club_is_visible_to_them(self):
        self.create(visibility_level='district')
        club = Club.objects.get(name='New Club')
        self.assertEqual(
            club.members.get(user=self.creator).role, ClubMember.Role.OWNER,
        )
        self.assertTrue(visibility.can_see(club, self.place_of(self.creator)))


# ─── 12: the clubs that already existed ───────────────────────────────────────
class LegacyClubMigrationTests(GeoFixture):
    """The data migration, run the way the migration runs it.

    `mark_existing_as_global` only reads a model and updates one column, so
    calling it with the live app registry exercises the real function rather
    than a paraphrase of it.
    """

    def _pre_existing_club(self):
        """A club as it looked before this feature: no location at all."""
        creator = self.make_user('legacy-owner', at=self.IN_ORBITA)
        member = self.make_user('legacy-member', at=self.IN_ASTANA)
        club = Club.objects.create(name='Founded Long Ago', creator=creator)
        ClubMember.objects.create(user=creator, club=club, role=ClubMember.Role.OWNER)
        ClubMember.objects.create(user=member, club=club)
        return club, creator, member

    # 12
    def test_existing_clubs_survive_and_stay_visible_everywhere(self):
        club, creator, member = self._pre_existing_club()
        # Before the data migration the new column's default has already made
        # it invisible — which is precisely why the migration exists.
        self.assertEqual(club.visibility_level, Club.Visibility.CITY)
        self.assertFalse(visibility.can_see(club, self.place_of(creator)))

        legacy_migration.mark_existing_as_global(django_apps, None)
        club.refresh_from_db()

        self.assertEqual(club.visibility_level, Club.Visibility.GLOBAL)
        for user in (creator, member, self.make_user('anyone', at=self.IN_ZHETYSU)):
            self.assertTrue(visibility.can_see(club, self.place_of(user)))
        # …and for somebody we cannot place at all.
        self.assertTrue(
            visibility.can_see(club, self.place_of(self.make_user('nomad')))
        )

    def test_migration_does_not_touch_memberships_or_scoped_clubs(self):
        club, creator, member = self._pre_existing_club()
        home = self.place_of(creator)
        scoped = self.make_club('Orbita Runners', 'district', home)

        legacy_migration.mark_existing_as_global(django_apps, None)

        self.assertEqual(club.members.count(), 2)
        self.assertEqual(
            set(club.members.values_list('user_id', flat=True)),
            {creator.pk, member.pk},
        )
        scoped.refresh_from_db()
        self.assertEqual(scoped.visibility_level, 'district')
        self.assertEqual(scoped.visibility_district_id, self.orbita.pk)

    def test_reverse_puts_the_default_back(self):
        club, _, _ = self._pre_existing_club()
        legacy_migration.mark_existing_as_global(django_apps, None)
        legacy_migration.restore_default(django_apps, None)
        club.refresh_from_db()
        self.assertEqual(club.visibility_level, Club.Visibility.CITY)

    def test_backfill_scopes_a_legacy_club_to_its_creators_city(self):
        """`scope_legacy_clubs` is how the unscoped set shrinks.

        City level, never narrower: the creator's position a year on is
        evidence for which city the club is in and no evidence at all for which
        street corner it meets on.
        """
        club, creator, member = self._pre_existing_club()
        legacy_migration.mark_existing_as_global(django_apps, None)
        # Both in Almaty, so there is no disagreement to stop the backfill.
        self.stand(member, self.IN_KAZAKHFILM)

        call_command('scope_legacy_clubs', stdout=StringIO())
        club.refresh_from_db()
        self.assertEqual(club.visibility_level, Club.Visibility.GLOBAL)  # dry run

        call_command('scope_legacy_clubs', '--apply', stdout=StringIO())
        club.refresh_from_db()

        self.assertEqual(club.visibility_level, Club.Visibility.CITY)
        self.assertEqual(club.visibility_city_id, self.almaty.pk)
        self.assertEqual(club.visibility_city_osm, self.almaty.osm_ref)
        self.assertTrue(visibility.can_see(club, self.place_of(creator)))
        self.assertTrue(visibility.can_see(club, self.place_of(member)))
        # Now scoped, so it is no longer visible from the other end of the country.
        self.assertFalse(
            visibility.can_see(
                club, self.place_of(self.make_user('astana', at=self.IN_ASTANA)))
        )

    def test_backfill_skips_a_club_whose_creator_moved_away_from_its_members(self):
        """The creator emigrating is not the club emigrating."""
        club, creator, member = self._pre_existing_club()
        legacy_migration.mark_existing_as_global(django_apps, None)
        # Creator is in Astana; every placed member is in Almaty.
        self.stand(creator, self.IN_ASTANA)
        self.stand(member, self.IN_ORBITA)

        out = StringIO()
        call_command('scope_legacy_clubs', '--apply', stdout=out)

        club.refresh_from_db()
        self.assertEqual(club.visibility_level, Club.Visibility.GLOBAL)
        self.assertIn('may have moved', out.getvalue())

    def test_backfill_skips_a_club_whose_creator_cannot_be_placed(self):
        creator = self.make_user('ghost-owner')  # never reported a position
        club = Club.objects.create(name='Orphan', creator=creator)
        ClubMember.objects.create(user=creator, club=club, role=ClubMember.Role.OWNER)
        legacy_migration.mark_existing_as_global(django_apps, None)

        call_command('scope_legacy_clubs', '--apply', stdout=StringIO())

        club.refresh_from_db()
        self.assertEqual(club.visibility_level, Club.Visibility.GLOBAL)

    def test_backfill_leaves_already_scoped_clubs_alone(self):
        creator = self.make_user('creator', at=self.IN_ORBITA)
        scoped = self.make_club(
            'Orbita Runners', 'district', self.place_of(creator), creator=creator)

        call_command('scope_legacy_clubs', '--apply', stdout=StringIO())

        scoped.refresh_from_db()
        self.assertEqual(scoped.visibility_level, 'district')
        self.assertEqual(scoped.visibility_district_id, self.orbita.pk)

    def test_legacy_clubs_are_not_offered_as_a_choice(self):
        self.assertNotIn(Club.Visibility.GLOBAL, Club.SELECTABLE)
        creator = self.make_user('creator', at=self.IN_ORBITA)
        response = self.client_for(creator).post(
            '/api/clubs/', {'name': 'Everywhere', 'visibility_level': 'global'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)


# ─── 13: members, owners and the people who run the place ─────────────────────
class MembershipOverrideTests(GeoFixture):
    """Location governs discovery; belonging governs access.

    These assertions pin that decision down, so changing it means changing a
    test that says what it costs.
    """

    def setUp(self):
        super().setUp()
        self.owner = self.make_user('owner', at=self.IN_ORBITA)
        self.member = self.make_user('member', at=self.IN_ORBITA)
        home = self.place_of(self.owner)
        self.club = self.make_club(
            'Orbita Runners', 'district', home, creator=self.owner)
        ClubMember.objects.create(user=self.member, club=self.club)

    def travel(self, user):
        return self.stand(user, self.IN_ASTANA)

    # 13
    def test_owner_can_still_manage_from_another_city(self):
        self.travel(self.owner)
        client = self.client_for(self.owner)

        self.assertEqual(client.get(f'/api/clubs/{self.club.pk}/').status_code, 200)
        self.assertEqual(
            client.get(f'/api/clubs/{self.club.pk}/members/').status_code, 200)
        self.assertEqual(
            client.get(f'/api/clubs/{self.club.pk}/messages/').status_code, 200)

        promoted = client.patch(
            f'/api/clubs/{self.club.pk}/promote-owner/{self.member.pk}/')
        self.assertEqual(promoted.status_code, 200)

        intruder = self.make_user('intruder', at=self.IN_ORBITA)
        ClubMember.objects.create(user=intruder, club=self.club)
        kicked = client.delete(f'/api/clubs/{self.club.pk}/kick/{intruder.pk}/')
        self.assertEqual(kicked.status_code, 204)

    def test_creator_can_delete_from_anywhere(self):
        self.travel(self.owner)
        response = self.client_for(self.owner).delete(f'/api/clubs/{self.club.pk}/')
        self.assertEqual(response.status_code, 204)

    def test_member_keeps_access_after_leaving_the_area(self):
        self.travel(self.member)
        client = self.client_for(self.member)
        self.assertEqual(client.get(f'/api/clubs/{self.club.pk}/').status_code, 200)
        self.assertEqual(
            client.get(f'/api/clubs/{self.club.pk}/messages/').status_code, 200)

        posted = client.post(
            f'/api/clubs/{self.club.pk}/messages/', {'content': 'still here'},
            format='json',
        )
        self.assertEqual(posted.status_code, 201)

    def test_joined_list_travels_with_the_member(self):
        self.travel(self.member)
        joined = self.client_for(self.member).get('/api/clubs/?joined=true').data
        self.assertEqual([c['name'] for c in joined], ['Orbita Runners'])

    def test_membership_does_not_put_the_club_back_into_discovery(self):
        """The one thing membership must *not* do."""
        self.travel(self.member)
        discover = self.client_for(self.member).get('/api/clubs/').data
        self.assertEqual(list(discover), [])

    def test_leaving_ends_the_override(self):
        self.travel(self.member)
        left = self.client_for(self.member).post(f'/api/clubs/{self.club.pk}/leave/')
        self.assertEqual(left.status_code, 204)
        self.assertEqual(
            self.client_for(self.member).get(f'/api/clubs/{self.club.pk}/').status_code,
            404,
        )


# ─── 14: the list a phone is still holding ────────────────────────────────────
class LocationChangeTests(GeoFixture):
    # 14
    def test_a_cached_list_stops_working_when_the_user_moves(self):
        local = self.make_user('local', at=self.IN_ORBITA)
        home = self.place_of(local)
        self.make_club('Orbita Runners', 'district', home)

        client = self.client_for(local)
        listed = client.get('/api/clubs/').data
        self.assertEqual([c['name'] for c in listed], ['Orbita Runners'])
        club_id = listed[0]['id']

        # The phone still has that list on screen when its owner lands in
        # Astana. Every route the stale list can reach has to refuse.
        self.stand(local, self.IN_ASTANA)

        self.assertEqual(list(client.get('/api/clubs/').data), [])
        self.assertEqual(client.get(f'/api/clubs/{club_id}/').status_code, 404)
        self.assertEqual(client.post(f'/api/clubs/{club_id}/join/').status_code, 404)
        self.assertEqual(
            client.get(f'/api/clubs/{club_id}/members/').status_code, 404)

    def test_moving_district_swaps_which_clubs_are_visible(self):
        local = self.make_user('local', at=self.IN_ORBITA)
        orbita_club = self.make_club(
            'Orbita Runners', 'district', self.place_of(local))

        neighbour = self.make_user('neighbour', at=self.IN_KAZAKHFILM)
        kazakhfilm_club = self.make_club(
            'Kazakhfilm Cinema', 'district', self.place_of(neighbour))

        client = self.client_for(local)
        self.assertEqual(
            {c['name'] for c in client.get('/api/clubs/').data},
            {'Orbita Runners'},
        )

        self.stand(local, self.IN_KAZAKHFILM)
        self.assertEqual(
            {c['name'] for c in client.get('/api/clubs/').data},
            {'Kazakhfilm Cinema'},
        )
        self.assertFalse(visibility.can_see(orbita_club, self.place_of(local)))
        self.assertTrue(visibility.can_see(kazakhfilm_club, self.place_of(local)))

    def test_snapshot_survives_movement_inside_the_same_district(self):
        """No re-resolve while the user is still inside what we recorded.

        Also the border case: someone walking a district's edge must not flip
        tiers every time their position wobbles.
        """
        local = self.make_user('local', at=self.IN_ORBITA)
        before = UserPlace.objects.get(user=local)

        local.latitude, local.longitude = 0.15, 0.15  # still inside Orbita
        local.save(update_fields=['latitude', 'longitude'])
        after = places.snapshot_for(local)

        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.district_id, self.orbita.pk)
        # Not recomputed: the stored position is the one it was derived from.
        self.assertAlmostEqual(after.latitude, 0.1)

    def test_snapshot_is_recomputed_once_the_user_leaves(self):
        local = self.make_user('local', at=self.IN_ORBITA)
        local.latitude, local.longitude = 0.4, 0.4  # Kazakhfilm
        local.save(update_fields=['latitude', 'longitude'])

        after = places.snapshot_for(local)
        self.assertEqual(after.district_id, self.kazakhfilm.pk)
        self.assertEqual(after.region_id, self.bostandyk.pk)

    def test_location_report_keeps_the_snapshot_current(self):
        local = self.make_user('local', at=self.IN_ORBITA)
        response = self.client_for(local).patch(
            '/api/proximity/location/',
            {'latitude': 0.7, 'longitude': 0.7, 'accuracy': 12.0},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

        place = UserPlace.objects.get(user=local)
        self.assertEqual(place.region_id, self.almaly.pk)
        self.assertEqual(place.district_id, self.zhetysu.pk)
        local.refresh_from_db()
        self.assertEqual(local.location_accuracy_m, 12.0)
        self.assertIsNotNone(local.location_updated_at)


# ─── A map that changes under the feature ─────────────────────────────────────
class MapChangesTests(GeoFixture):
    """Boundaries get cached, evicted and re-fetched while clubs sit still.

    The failure these guard against is the quiet one: a club that stops being
    visible to anybody, in only the places where the map improved, with nothing
    in the logs. It is why visibility asks "is this user inside the boundary
    the club named" rather than "is that boundary still in the same tier slot".
    """

    def test_a_newly_mapped_district_does_not_unscope_an_existing_club(self):
        """The regression this design exists for.

        A club is scoped to Almaly, which at the time is the only sub-area
        anyone has cached there — so it sits in the *region* tier. Later
        somebody caches Zhetysu inside it, and Almaly is still the region tier
        but the chain is now two deep. Under tier-slot matching a club scoped
        to the old narrowest area would fall out of its own slot; under
        containment it cannot.
        """
        self.zhetysu.delete()  # not mapped yet
        local = self.make_user('local', at=self.IN_ZHETYSU)
        place = self.place_of(local)
        self.assertEqual(place.region_id, self.almaly.pk)
        self.assertIsNone(place.district_id)

        club = self.make_club('Almaly Chess', 'region', place)
        self.assertTrue(visibility.can_see(club, place))

        # Somebody maps the microdistrict. The chain deepens under everyone.
        zhetysu = make_region(
            'Zhetysu', (0.6, 0.6, 0.8, 0.8), Region.Kind.DISTRICT, 9)
        self.stand(local, self.IN_ZHETYSU)
        after = self.place_of(local)
        self.assertEqual(after.district_id, zhetysu.pk)
        self.assertEqual(after.region_id, self.almaly.pk)

        self.assertTrue(visibility.can_see(club, after))
        self.assertTrue(
            visibility.visible_to(Club.objects.all(), after)
            .filter(pk=club.pk).exists()
        )

    def test_a_club_in_the_middle_of_a_deep_chain_still_matches(self):
        """Four nested areas, three tiers. The one that gets no tier still works.

        Bostandyk sits between Almaty and Orbita here, and with an extra level
        below it there are more shapes than slots. A club scoped to it is
        matched on containment, so being un-tiered costs it nothing.
        """
        block = make_region('Block 12', (0, 0, 0.05, 0.05), Region.Kind.DISTRICT, 11)
        local = self.make_user('local', at=(0.02, 0.02))
        place = self.place_of(local)

        # Tiers take the ends of the chain; Bostandyk and Orbita share the middle.
        self.assertEqual(place.district_id, block.pk)
        self.assertEqual(place.region_id, self.bostandyk.pk)
        self.assertIn(self.orbita.pk, place.area_ids)

        club = self.make_club(
            'Orbita Runners', 'district', place,
            visibility_district=self.orbita,
            visibility_district_osm=self.orbita.osm_ref,
            visibility_district_name='Orbita',
        )
        self.assertTrue(visibility.can_see(club, place))
        self.assertTrue(
            visibility.visible_to(Club.objects.all(), place)
            .filter(pk=club.pk).exists()
        )

        # Still nothing to do with the district next door.
        self.assertFalse(
            visibility.can_see(
                club, self.place_of(self.make_user('kfilm', at=self.IN_KAZAKHFILM)))
        )

    def test_the_chain_records_everything_containing_the_user(self):
        local = self.make_user('local', at=self.IN_ORBITA)
        place = self.place_of(local)
        self.assertEqual(
            set(place.area_ids), {self.almaty.pk, self.bostandyk.pk, self.orbita.pk},
        )
        self.assertEqual(
            set(place.area_osm_refs),
            {self.almaty.osm_ref, self.bostandyk.osm_ref, self.orbita.osm_ref},
        )
        # Never anything it is not inside.
        self.assertNotIn(self.astana.pk, place.area_ids)
        self.assertNotIn(self.kazakhfilm.pk, place.area_ids)


class DeepenChainTests(GeoFixture):
    """The explicit sweep for boundaries the resolver would never fetch.

    The resolver stops as soon as it is confident, which is right for "which
    district am I in" and wrong for "how many nested areas are there". Where
    the raion answers at stage 3, the microdistrict inside it is never asked
    for — so a creator standing in one would be told no district is mapped.
    """

    def setUp(self):
        super().setUp()
        # A city with one known sub-area; the finer one exists only upstream.
        self.gorod = make_region('Gorod', (50, 50, 51, 51), Region.Kind.CITY, 4)
        self.raion = make_region(
            'Raion', (50, 50, 50.5, 50.5), Region.Kind.DISTRICT, 6)
        self.AT = (50.1, 50.1)

    def _upstream_has(self, region):
        """Overpass reports a relation; nominatim.lookup materialises it."""
        boundary = type('B', (), {'osm_ref': region.osm_ref, 'osm_id': region.osm_id})()
        return (
            patch('regions.overpass.boundaries_at', return_value=[boundary]),
            patch('regions.nominatim.lookup', return_value=[region]),
        )

    def test_the_sweep_finds_a_district_the_resolver_stops_short_of(self):
        # Mapped upstream but not stored, exactly as it would be before anyone
        # in this city had opened the create screen.
        micro = make_region(
            'Micro', (50, 50, 50.2, 50.2), Region.Kind.DISTRICT, 9)
        micro_pk = micro.pk
        Region.objects.filter(pk=micro_pk).delete()

        user = self.make_user('creator', at=self.AT)
        cheap = self.place_of(user)
        self.assertEqual(cheap.region_id, self.raion.pk)
        self.assertIsNone(cheap.district_id)  # nothing finer stored yet

        micro.pk = None
        micro.save()
        boundaries, lookup = self._upstream_has(micro)
        with boundaries, lookup as mock_lookup:
            place = visibility.place_for(user, allow_upstream=True, force=True)

        self.assertTrue(mock_lookup.called)
        self.assertEqual(place.district_id, micro.pk)
        self.assertEqual(place.region_id, self.raion.pk)
        self.assertEqual(
            visibility.available_levels(place), ['district', 'region', 'city'],
        )

    def test_the_sweep_does_not_refetch_what_is_already_stored(self):
        boundary = type('B', (), {
            'osm_ref': self.raion.osm_ref, 'osm_id': self.raion.osm_id,
        })()
        user = self.make_user('creator', at=self.AT)
        with patch('regions.overpass.boundaries_at', return_value=[boundary]), \
                patch('regions.nominatim.lookup') as mock_lookup:
            visibility.place_for(user, allow_upstream=True, force=True)
        self.assertFalse(mock_lookup.called)

    def test_a_failing_sweep_is_a_narrower_menu_not_an_error(self):
        from regions import upstream as regions_upstream

        user = self.make_user('creator', at=self.AT)
        with patch(
            'regions.overpass.boundaries_at',
            side_effect=regions_upstream.UpstreamError('overpass down'),
        ):
            place = visibility.place_for(user, allow_upstream=True, force=True)

        self.assertTrue(place.resolved)
        self.assertEqual(visibility.available_levels(place), ['region', 'city'])


# ─── Borders, fuzz and the shape of the data ──────────────────────────────────
class BoundaryConditionTests(GeoFixture):
    def test_approximate_sharing_does_not_push_someone_out_of_their_district(self):
        """The fuzz is ours, so the tolerance is ours to allow for.

        An Approximate sharer's stored position is snapped to a ~500m grid
        before it is written, which near a border lands outside the district
        they are standing in. Testing that strictly is the bug
        users/serializers.py already documents for zones.
        """
        user = self.make_user('approx')
        user.location_sharing = User.LocationSharing.APPROXIMATE
        # Just outside Orbita's eastern edge — well within the grid error.
        user.latitude, user.longitude = 0.1, 0.2009
        user.location_updated_at = timezone.now()
        user.save()

        place = places.snapshot_for(user, force=True)
        self.assertEqual(place.district_id, self.orbita.pk)

        precise = self.make_user('precise')
        precise.latitude, precise.longitude = 0.1, 0.2009
        precise.location_updated_at = timezone.now()
        precise.save()
        # Same coordinates, no fuzz to allow for: strictly outside.
        self.assertNotEqual(
            places.snapshot_for(precise, force=True).district_id, self.orbita.pk,
        )

    def test_two_districts_with_the_same_name_in_different_cities(self):
        """Names are never compared across cities, because the city gates first."""
        twin_city = make_region('Twin', (10, 10, 11, 11), Region.Kind.CITY, 4)
        make_region('Orbita', (10, 10, 10.2, 10.2), Region.Kind.DISTRICT, 9)
        make_region('Twin Central', (10, 10, 10.5, 10.5), Region.Kind.DISTRICT, 6)

        here = self.make_user('here', at=self.IN_ORBITA)
        there = self.make_user('there', at=(10.1, 10.1))
        club = self.make_club('Orbita Runners', 'district', self.place_of(here))

        far = self.place_of(there)
        self.assertEqual(far.city_id, twin_city.pk)
        self.assertEqual(far.district_name, 'Orbita')  # same word, other city
        self.assertFalse(visibility.can_see(club, far))

    def test_a_city_with_no_sub_areas_offers_only_city(self):
        make_region('Lone', (20, 20, 21, 21), Region.Kind.CITY, 8, country='fr')
        user = self.make_user('lone', at=(20.5, 20.5))
        place = self.place_of(user)
        self.assertTrue(place.resolved)
        self.assertIsNone(place.region_id)
        self.assertIsNone(place.district_id)
        self.assertEqual(visibility.available_levels(place), ['city'])

    def test_one_sub_area_fills_the_region_tier_not_the_district(self):
        """Two shapes, two tiers. Never three from two.

        Assigning the lone sub-area to `district` instead would let somebody
        scope a club to a boundary nobody else's lookup would place them in.
        """
        astana_user = self.make_user('astana', at=self.IN_ASTANA)
        place = self.place_of(astana_user)
        self.assertEqual(place.region_id, self.yesil.pk)
        self.assertIsNone(place.district_id)

    def test_antimeridian_city_places_its_residents(self):
        """A region stored in an unwrapped window still contains its own people."""
        suva = make_region(
            'Suva', (179.5, -18.5, 180.5, -17.5), Region.Kind.CITY, 8, country='fj')
        make_region(
            'Nasese', (179.9, -18.2, 180.2, -18.0), Region.Kind.DISTRICT, 9,
            country='fj')

        # -179.95 is the ordinary spelling of 180.05.
        user = self.make_user('fiji', at=(-18.1, -179.95))
        place = self.place_of(user)
        self.assertEqual(place.city_id, suva.pk)
        self.assertEqual(place.region_name, 'Nasese')

        club = self.make_club('Suva Surf', 'region', place)
        self.assertTrue(visibility.can_see(club, place))
        self.assertFalse(
            visibility.can_see(
                club, self.place_of(self.make_user('kz', at=self.IN_ORBITA)))
        )

    def test_polygon_containment_not_bounding_box(self):
        """A point in the bounding box but outside the shape is outside.

        An L-shaped district is the cheap way to prove the ring walk is really
        running: the notch is inside the box and outside the region.
        """
        make_region('Elbow City', (30, 30, 31, 31), Region.Kind.CITY, 8, country='pt')
        make_region(
            'Elbow', None, Region.Kind.DISTRICT, 9, country='pt',
            rings=[[
                [30.0, 30.0], [30.4, 30.0], [30.4, 30.2],
                [30.2, 30.2], [30.2, 30.4], [30.0, 30.4], [30.0, 30.0],
            ]],
        )

        inside = self.place_of(self.make_user('inside', at=(30.1, 30.1)))
        notch = self.place_of(self.make_user('notch', at=(30.3, 30.3)))
        self.assertEqual(inside.region_name, 'Elbow')
        self.assertIsNone(notch.region_id)  # in the box, outside the polygon
        self.assertEqual(notch.city_name, 'Elbow City')

        club = self.make_club('Elbow Runners', 'region', inside)
        self.assertTrue(visibility.can_see(club, inside))
        self.assertFalse(visibility.can_see(club, notch))
