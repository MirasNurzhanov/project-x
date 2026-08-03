"""District resolution across administrative systems that do not resemble
each other.

Each country here is a different answer to "what is the thing inside a city".
A London borough, a Mumbai ward, a Nairobi ward, a Tanzanian kata, a Vietnamese
phường, an Osaka 区 and an Almaty район are all "my district" to the person
standing in one, and OSM files them at admin levels between 6 and 10 with no
agreement between countries about which.

The point of these fixtures is that no single rule could pass all of them. What
passes them is the pipeline: containment decides what is possible, the address
hierarchy decides what is likely, the per-country level table ranks what is
left, and anything that survives none of it is handed back as a question rather
than an answer.

The United States is here as a *negative*: its neighbourhoods are mapped as
nodes, so the honest outcome is that nothing is found and the county sitting
above is refused rather than substituted.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from . import admin_levels, overpass, resolver
from .models import Region
from .resolver import Outcome
from .test_geometry import FIJI
from .tests import SQUARE, make_region, make_user

BIG = [[-1, -1], [3, -1], [3, 3], [-1, 3], [-1, -1]]
HUGE = [[-20, -20], [20, -20], [20, 20], [-20, 20], [-20, -20]]
# A district-sized shape straddling the dateline — a tenth of a degree across,
# unlike the country-sized FIJI fixture.
SMALL_CROSSING = [
    [179.95, -17.55], [-179.95, -17.55], [-179.95, -17.45],
    [179.95, -17.45], [179.95, -17.55],
]


def boundary(name, osm_id, addresstype, admin_level=None, ring=None,
             display=None, cc='xx'):
    """One Nominatim search result, shaped the way the real one is."""
    return {
        'osm_type': 'relation', 'osm_id': osm_id, 'name': name,
        'display_name': display or f'{name}, Somewhere',
        'addresstype': addresstype, 'category': 'boundary',
        'extratags': {'admin_level': str(admin_level)} if admin_level else {},
        'geojson': {'type': 'Polygon', 'coordinates': [ring or SQUARE]},
        'address': {'country_code': cc},
    }


@override_settings(OVERPASS_ENABLED=False)
class CountryFixtureTests(TestCase):
    """One country per test, each with its own idea of what a district is."""

    def setUp(self):
        cache.clear()

    def resolve(self, address, *searches, kind='district', lat=0.5, lon=0.5):
        """Drive the pipeline against a fixed outside world.

        Searches are padded with empty results so a test only has to state the
        responses it cares about — the resolver may legitimately try more names
        than a fixture bothers to describe.
        """
        responses = [{'address': address}] + list(searches) + [[], [], [], []]
        with patch('regions.nominatim._get', side_effect=responses):
            return resolver.resolve(lat, lon, kind=kind)

    # ── United Kingdom ────────────────────────────────────────────────────────
    def test_united_kingdom_borough(self):
        """A London borough is level 8, and is what a Londoner means by their
        district — the ward below it is a few thousand people."""
        result = self.resolve(
            {
                'suburb': 'Bloomsbury', 'borough': 'Camden', 'city': 'London',
                'state': 'England', 'country_code': 'gb',
            },
            [boundary('Camden', 6001, 'borough', admin_level=8,
                      display='Camden, London, England', cc='gb')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Camden')
        self.assertEqual(result.region.kind, 'district')

    def test_united_kingdom_city_is_greater_london_not_the_borough(self):
        result = self.resolve(
            {'borough': 'Camden', 'city': 'London', 'country_code': 'gb'},
            [boundary('London', 6002, 'city', admin_level=6,
                      display='London, England', cc='gb')],
            kind='city',
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'London')

    # ── India ─────────────────────────────────────────────────────────────────
    def test_india_ward(self):
        """India's `district` is the zila, far larger than a city, so the thing
        somebody means is the municipal ward — level 10."""
        result = self.resolve(
            {
                'suburb': 'Bandra West', 'city_district': 'Bandra',
                'city': 'Mumbai', 'state': 'Maharashtra', 'country_code': 'in',
            },
            [boundary('Bandra West', 6010, 'suburb', admin_level=10,
                      display='Bandra West, Mumbai, Maharashtra', cc='in')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Bandra West')

    # ── Kenya ─────────────────────────────────────────────────────────────────
    def test_kenya_ward(self):
        """Nairobi is a county (level 4); its wards are level 7."""
        result = self.resolve(
            {'ward': 'Kilimani', 'city': 'Nairobi', 'country_code': 'ke'},
            [boundary('Kilimani', 6020, 'ward', admin_level=7,
                      display='Kilimani, Nairobi', cc='ke')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.kind, 'district')

    # ── Tanzania ──────────────────────────────────────────────────────────────
    def test_tanzania_kata(self):
        """The ward (kata) is the everyday unit, and `ward` was the address key
        missing entirely before this work — the boundary was mapped the whole
        time and simply never reached."""
        result = self.resolve(
            {'ward': 'Msasani', 'city': 'Dar es Salaam', 'country_code': 'tz'},
            [boundary('Msasani', 6030, 'ward', admin_level=7,
                      display='Msasani, Dar es Salaam', cc='tz')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Msasani')

    # ── Vietnam ───────────────────────────────────────────────────────────────
    def test_vietnam_phuong(self):
        """A phường is level 8. Non-ASCII names have to survive the round trip
        through the search query intact."""
        result = self.resolve(
            {
                'ward': 'Phường Bến Nghé', 'city': 'Hồ Chí Minh',
                'country_code': 'vn',
            },
            [boundary('Phường Bến Nghé', 6040, 'ward', admin_level=8,
                      display='Phường Bến Nghé, Hồ Chí Minh', cc='vn')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Phường Bến Nghé')

    # ── Japan ─────────────────────────────────────────────────────────────────
    def test_japan_ward_of_a_designated_city(self):
        """Osaka's 区 are level 8. Tokyo's special wards are level 7 and are
        cities in their own right, which is why 7 is the *city* level for jp."""
        result = self.resolve(
            {
                'city_district': 'Kita-ku', 'city': 'Osaka',
                'province': 'Osaka Prefecture', 'country_code': 'jp',
            },
            [boundary('Kita-ku', 6050, 'city_district', admin_level=8,
                      display='Kita-ku, Osaka', cc='jp')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Kita-ku')

    def test_japan_city_level_is_seven(self):
        self.assertEqual(admin_levels.levels_for('jp', 'city'), (7,))

    # ── United States ─────────────────────────────────────────────────────────
    def test_united_states_county_is_never_offered_as_a_district(self):
        """The honest US outcome. Neighbourhoods there are overwhelmingly
        mapped as nodes, so the hierarchy names the county instead — and a
        county is two orders of magnitude too big. Refusing it and reporting
        nothing is correct; substituting it would silently blanket a metro."""
        result = self.resolve(
            {
                'county': 'Multnomah County', 'city': 'Portland',
                'state': 'Oregon', 'country_code': 'us',
            },
            [boundary('Multnomah County', 6060, 'administrative', admin_level=6,
                      ring=HUGE, display='Multnomah County, Oregon', cc='us')],
        )
        self.assertFalse(result.resolved)
        self.assertEqual(result.outcome, Outcome.UNMAPPED)
        self.assertEqual(result.city_name, 'Portland')

    def test_united_states_city_still_resolves(self):
        result = self.resolve(
            {'city': 'Portland', 'state': 'Oregon', 'country_code': 'us'},
            [boundary('Portland', 6061, 'city', admin_level=8,
                      display='Portland, Oregon', cc='us')],
            kind='city',
        )
        self.assertTrue(result.resolved)

    # ── Kazakhstan ────────────────────────────────────────────────────────────
    def test_kazakhstan_district_is_level_six(self):
        """The collision the whole level table exists for: Almaty the *city* is
        level 4, the same as an oblast, and its districts are level 6 — the
        level that means "county" almost everywhere else."""
        result = self.resolve(
            {
                'city_district': 'Almaly', 'city': 'Almaty', 'country_code': 'kz',
            },
            [boundary('Almaly', 6070, 'city_district', admin_level=6,
                      display='Almaly, Almaty', cc='kz')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Almaly')

    def test_kazakhstan_city_at_level_four_is_not_too_coarse(self):
        """Level 4 is a state nearly everywhere and a city here. The veto has
        to know the difference or Almaty can never be anybody's city."""
        self.assertFalse(admin_levels.is_too_coarse('kz', 'city', 4))
        self.assertTrue(admin_levels.is_too_coarse('gb', 'city', 4))

    # ── United Arab Emirates ──────────────────────────────────────────────────
    def test_uae_community(self):
        """Dubai's communities sit at 7 or 8 depending on the emirate, which is
        why both are listed and why the name match carries the weight here."""
        result = self.resolve(
            {'suburb': 'Al Barsha', 'city': 'Dubai', 'country_code': 'ae'},
            [boundary('Al Barsha', 6080, 'suburb', admin_level=7,
                      display='Al Barsha, Dubai', cc='ae')],
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Al Barsha')

    # ── Antimeridian ──────────────────────────────────────────────────────────
    def test_fiji_district_across_the_dateline(self):
        """Everything above, for a shape whose longitudes run past 180."""
        result = self.resolve(
            {'suburb': 'Nasese', 'city': 'Suva', 'country_code': 'fj'},
            [boundary('Nasese', 6090, 'suburb', admin_level=9, ring=FIJI,
                      display='Nasese, Suva', cc='fj')],
            lat=-17.5, lon=-179.5,
        )
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Nasese')
        self.assertTrue(result.region.crosses_antimeridian)
        # And it really does contain the point it was resolved for, from the
        # far side of the line.
        self.assertTrue(result.region.contains(-17.5, -179.5))
        self.assertTrue(result.region.contains(-17.5, 179.5))

    def test_an_unlisted_country_still_resolves_on_the_other_signals(self):
        """No level table entry, so the nominal scheme applies and the ranking
        leans on name and hierarchy. A country we have never heard of is not a
        country the feature refuses to work in."""
        result = self.resolve(
            {'suburb': 'Vaitele', 'city': 'Apia', 'country_code': 'ws'},
            [boundary('Vaitele', 6100, 'suburb', admin_level=9,
                      display='Vaitele, Apia', cc='ws')],
        )
        self.assertTrue(result.resolved)


# ─── Stages ───────────────────────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class PipelineStageTests(TestCase):
    """Which stage answered, and that the cheap ones come first."""

    def setUp(self):
        cache.clear()

    def test_a_stored_polygon_answers_without_any_upstream_call(self):
        make_region(
            osm_id=6200, name='Old Town', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        with patch('regions.nominatim._get') as get:
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertTrue(result.resolved)
        self.assertEqual(result.stage, 'cache')
        self.assertEqual(result.outcome, Outcome.RESOLVED)
        get.assert_not_called()

    def test_the_name_the_reverse_geocode_pointed_at_is_the_ordinary_path(self):
        """Not a fallback. The hierarchy named this level for this point and a
        boundary of that name was there — that is the lookup working, and the
        coverage figures should not read as though it limped."""
        responses = [
            {'address': {'ward': 'Msasani', 'city': 'Dar', 'country_code': 'tz'}},
            [boundary('Msasani', 6210, 'ward', admin_level=7, cc='tz')],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertEqual(result.outcome, Outcome.RESOLVED)
        self.assertEqual(result.stage, 'reverse')

    def test_a_later_name_is_reported_as_a_fallback(self):
        """The first name found nothing and the second did. That is the
        pipeline casting about, and it is worth being able to count."""
        responses = [
            {
                'address': {
                    'city_district': 'Nowhere', 'suburb': 'Msasani',
                    'city': 'Dar', 'country_code': 'tz',
                },
            },
            [],
            [boundary('Msasani', 6211, 'ward', admin_level=7, cc='tz')],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertEqual(result.outcome, Outcome.RESOLVED_FALLBACK)
        self.assertEqual(result.stage, 'name_search')

    def test_all_the_collected_names_are_available_for_searching(self):
        """The single-name version of this is what missed districts wholesale:
        one key present in the hierarchy and no polygon behind it ended the
        lookup, even with three other names sitting unused beside it."""
        responses = [
            {
                'address': {
                    'city_district': 'Nowhere', 'suburb': 'Msasani',
                    'city': 'Dar', 'country_code': 'tz',
                },
            },
            [],  # 'Nowhere, Dar' finds nothing
            [boundary('Msasani', 6220, 'ward', admin_level=7, cc='tz')],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Msasani')

    def test_the_name_budget_is_bounded(self):
        address = {
            'address': {
                'city_district': 'A', 'district': 'B', 'borough': 'C',
                'ward': 'D', 'suburb': 'E', 'city': 'Town', 'country_code': 'tz',
            },
        }
        with patch('regions.nominatim._get', side_effect=[address] + [[]] * 10) as get:
            resolver.resolve(0.5, 0.5, kind='district')
        # One address call plus at most MAX_NAME_SEARCHES searches.
        self.assertLessEqual(get.call_count, 1 + resolver.DEFAULT_MAX_NAME_SEARCHES)


class BoundaryRelationFallbackTests(TestCase):
    """Stage 4 — the one that needs no name at all."""

    def setUp(self):
        cache.clear()

    def test_relations_are_looked_up_when_the_hierarchy_names_nothing(self):
        """Dar es Salaam with a hierarchy that stops at the city. Overpass says
        which relation encloses the point; Nominatim supplies its polygon."""
        address = {'address': {'city': 'Dar es Salaam', 'country_code': 'tz'}}
        found = [overpass.Boundary(918273, 7, 'Msasani', {'admin_level': '7'})]
        with patch('regions.nominatim._get', side_effect=[
            address,
            [boundary('Msasani', 918273, 'ward', admin_level=7,
                      display='Msasani, Dar es Salaam', cc='tz')],
        ]), patch('regions.overpass.boundaries_at', return_value=found) as asked:
            result = resolver.resolve(0.5, 0.5, kind='district')

        self.assertTrue(result.resolved)
        self.assertEqual(result.stage, 'boundary_relations')
        self.assertEqual(result.outcome, Outcome.RESOLVED_FALLBACK)
        # And it asked for the levels Tanzania actually uses.
        self.assertEqual(asked.call_args.args[2], admin_levels.levels_for('tz', 'district'))

    def test_a_failing_fallback_does_not_fail_the_lookup(self):
        """It is a fallback. If Overpass is down, the answer is still whatever
        the earlier stages found — or an honest miss, never a 500."""
        address = {'address': {'city': 'Dar es Salaam', 'country_code': 'tz'}}
        with patch('regions.nominatim._get', side_effect=[address]), \
             patch('regions.overpass.boundaries_at',
                   side_effect=overpass.OverpassError('down')):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertEqual(result.outcome, Outcome.UNMAPPED)

    @override_settings(OVERPASS_ENABLED=False)
    def test_the_stage_can_be_switched_off(self):
        address = {'address': {'city': 'Dar es Salaam', 'country_code': 'tz'}}
        with patch('regions.nominatim._get', side_effect=[address]), \
             patch('regions.overpass.boundaries_at') as asked:
            resolver.resolve(0.5, 0.5, kind='district')
        asked.assert_not_called()


class OverpassQueryTests(SimpleTestCase):
    def test_the_query_asks_for_administrative_relations_at_given_levels(self):
        query = overpass._query(-6.75, 39.27, (7, 8))
        self.assertIn('is_in(-6.750000,39.270000)', query)
        self.assertIn('"boundary"="administrative"', query)
        self.assertIn('^(7|8)$', query)
        # Tags only: the geometry comes from Nominatim, and asking for it here
        # would turn a small response into a very large one.
        self.assertIn('out tags;', query)
        self.assertNotIn('out geom', query)

    def test_relations_are_returned_tightest_level_first(self):
        payload = {'elements': [
            {'type': 'relation', 'id': 1, 'tags': {'admin_level': '7', 'name': 'Ward'}},
            {'type': 'relation', 'id': 2, 'tags': {'admin_level': '6', 'name': 'District'}},
        ]}
        with patch('regions.overpass._fetch', return_value=payload):
            found = overpass.boundaries_at(-6.75, 39.27, (6, 7))
        self.assertEqual([b.admin_level for b in found], [7, 6])
        self.assertEqual(found[0].osm_ref, 'R1')

    def test_elements_at_unrequested_levels_are_dropped(self):
        payload = {'elements': [
            {'type': 'relation', 'id': 1, 'tags': {'admin_level': '2', 'name': 'Country'}},
            {'type': 'way', 'id': 9, 'tags': {'admin_level': '7'}},
        ]}
        with patch('regions.overpass._fetch', return_value=payload):
            self.assertEqual(overpass.boundaries_at(-6.75, 39.27, (7,)), [])


# ─── Confidence ───────────────────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class ConfidenceTests(TestCase):
    """When the pipeline refuses to choose, and what it hands back instead."""

    def setUp(self):
        cache.clear()
        self.address = {
            'address': {'suburb': 'Central', 'city': 'Town', 'country_code': 'tz'},
        }

    def test_two_equally_good_candidates_are_ambiguous(self):
        """Same size, same level, neither named by the hierarchy. There is no
        honest way to pick, so it does not pick."""
        make_region(
            osm_id=6300, name='North Quarter', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        make_region(
            osm_id=6301, name='South Quarter', kind='district',
            rings=[[[0.0, 0.0], [1.02, 0.0], [1.02, 1.02], [0.0, 1.02], [0.0, 0.0]]],
            admin_level=7, country_code='tz',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')

        self.assertEqual(result.outcome, Outcome.AMBIGUOUS)
        self.assertIsNone(result.region)
        self.assertEqual(len(result.candidates), 2)
        names = {c.region.name for c in result.candidates}
        self.assertEqual(names, {'North Quarter', 'South Quarter'})

    def test_a_named_candidate_beats_an_unnamed_one_of_the_same_size(self):
        """The tie above is broken the moment the hierarchy names one of them —
        which is the whole reason the address stage runs before ranking."""
        make_region(
            osm_id=6310, name='Central', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        make_region(
            osm_id=6311, name='Somewhere Else', kind='district',
            rings=[[[0.0, 0.0], [1.02, 0.0], [1.02, 1.02], [0.0, 1.02], [0.0, 0.0]]],
            admin_level=7, country_code='tz',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Central')

    def test_a_shape_with_no_evidence_behind_it_is_rejected_not_returned(self):
        """It contains the point and nothing else recommends it: no level this
        country uses, no name in the hierarchy, no size label. Returning it
        would be a guess that becomes permanent the moment it is stored."""
        make_region(
            osm_id=6320, name='Unknown Area', kind='other', rings=[BIG],
            admin_level=None, country_code='',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertEqual(result.outcome, Outcome.REJECTED)
        self.assertIsNone(result.region)
        self.assertEqual(len(result.candidates), 1)

    def test_candidates_come_back_ranked_and_explained(self):
        """Three shapes that cannot be separated at stage 1, so the address is
        fetched and the ranking gets something to work with. The reasons are
        part of the contract: a picker offering three names and no way to tell
        them apart has moved the problem to the user rather than solved it."""
        make_region(
            osm_id=6330, name='Central', kind='other', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        make_region(
            osm_id=6331, name='Outer', kind='other',
            rings=[[[0.0, 0.0], [1.05, 0.0], [1.05, 1.05], [0.0, 1.05], [0.0, 0.0]]],
            country_code='tz',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')

        scores = [c.score for c in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(result.candidates[0].region.name, 'Central')
        self.assertTrue(any('name matches' in r for r in result.candidates[0].reasons))

    def test_an_unclassified_cached_region_is_still_a_candidate(self):
        """`other` means "we could not read the size off the payload", not
        "this is not a place". Filtering those out of the cache query made a
        great many correctly-mapped districts permanently unreachable."""
        make_region(
            osm_id=6350, name='Central', kind='other', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Central')

    def test_the_city_itself_never_wins_a_district_lookup(self):
        """It contains the point, it is named in the hierarchy, and it is the
        wrong answer by orders of magnitude."""
        make_region(
            osm_id=6340, name='Town', kind='city', rings=[BIG],
            admin_level=6, country_code='tz',
        )
        with patch('regions.nominatim._get', side_effect=[self.address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assertNotIn('Town', [c.region.name for c in result.candidates])


# ─── Endpoint ─────────────────────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class ForPointOutcomeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user('me')
        self.client.force_authenticate(self.user)

    def test_ambiguity_comes_back_as_ranked_choices(self):
        make_region(
            osm_id=6400, name='North', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        make_region(
            osm_id=6401, name='South', kind='district',
            rings=[[[0.0, 0.0], [1.02, 0.0], [1.02, 1.02], [0.0, 1.02], [0.0, 0.0]]],
            admin_level=7, country_code='tz',
        )
        address = {'address': {'city': 'Town', 'country_code': 'tz'}}
        with patch('regions.nominatim._get', side_effect=[address] + [[]] * 5):
            response = self.client.get(
                '/api/regions/for-point/?lat=0.5&lon=0.5&level=district'
            )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['outcome'], 'ambiguous')
        self.assertNotIn('id', body)
        self.assertEqual(len(body['candidates']), 2)
        self.assertIn('region', body['candidates'][0])
        self.assertIn('reasons', body['candidates'][0])
        self.assertTrue(body['can_trace'])

    def test_a_resolved_answer_keeps_the_shape_it_always_had(self):
        """An existing client reads `id` off the top level of a 200. That must
        not change just because the pipeline behind it grew."""
        region = make_region(
            osm_id=6410, name='Central', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        with patch('regions.nominatim._get'):
            body = self.client.get(
                '/api/regions/for-point/?lat=0.5&lon=0.5&level=district'
            ).json()
        self.assertEqual(body['id'], region.pk)
        self.assertEqual(body['name'], 'Central')
        self.assertEqual(body['outcome'], 'resolved')


# ─── Hand-traced boundaries ───────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class TracedBoundaryTests(TestCase):
    """The end of the line for coverage.

    When OSM has nothing and no fallback found anything, the alternative to
    "you cannot have a zone here" is letting the person standing in the place
    say where it ends. Trusted narrowly, because one rough outline must not
    become everyone's district.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = make_user('me')
        self.user.latitude, self.user.longitude = 0.5, 0.5
        self.user.save()
        self.client.force_authenticate(self.user)

    def trace(self, rings=None, **payload):
        body = {
            'name': 'Msasani',
            'kind': 'district',
            'parent_name': 'Dar es Salaam',
            'rings': rings if rings is not None else [SQUARE],
        }
        body.update(payload)
        return self.client.post('/api/regions/trace/', body, format='json')

    def test_a_traced_outline_is_stored(self):
        response = self.trace()
        self.assertEqual(response.status_code, 201)
        region = Region.objects.get(pk=response.json()['id'])
        self.assertEqual(region.source, Region.Source.USER)
        self.assertEqual(region.submitted_by, self.user)
        self.assertEqual(region.parent_name, 'Dar es Salaam')
        self.assertIsNone(region.osm_id)
        self.assertTrue(region.contains(0.5, 0.5))

    def test_it_arrives_unverified(self):
        """Nobody has checked it yet, and until somebody does it is one
        person's opinion about where a district ends."""
        self.trace()
        self.assertFalse(Region.objects.get(source='user').verified)

    def test_an_outline_that_does_not_cover_you_is_refused(self):
        far = [[50, 50], [51, 50], [51, 51], [50, 51], [50, 50]]
        response = self.trace(rings=[far])
        self.assertEqual(response.status_code, 400)
        self.assertIn('where you are', str(response.json()))

    def test_an_outline_the_size_of_a_country_is_refused(self):
        """Tracing exists for shapes too small or too new to be in OSM. One
        this size is either a mistake or an attempt to blanket a region."""
        response = self.trace(rings=[HUGE])
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', str(response.json()))

    def test_a_pin_sized_outline_is_refused(self):
        tiny = [[0.5, 0.5], [0.5001, 0.5], [0.5001, 0.5001], [0.5, 0.5001], [0.5, 0.5]]
        response = self.trace(rings=[tiny])
        self.assertEqual(response.status_code, 400)
        self.assertIn('too small', str(response.json()))

    def test_a_ring_with_too_few_points_is_refused(self):
        self.assertEqual(self.trace(rings=[[[0, 0], [1, 1]]]).status_code, 400)

    def test_out_of_range_coordinates_are_refused(self):
        bad = [[0, 0], [1, 0], [1, 200], [0, 200], [0, 0]]
        self.assertEqual(self.trace(rings=[bad]).status_code, 400)

    def test_a_user_without_a_position_cannot_trace(self):
        self.user.latitude = self.user.longitude = None
        self.user.save()
        response = self.trace()
        self.assertEqual(response.status_code, 400)
        self.assertIn('location', str(response.json()).lower())

    def test_tracing_requires_authentication(self):
        self.assertEqual(
            APIClient().post('/api/regions/trace/', {}, format='json').status_code, 401,
        )

    def test_a_crossing_outline_is_stored_unwrapped(self):
        """Tracing has to survive the dateline like everything else.

        A district-sized shape rather than the country-sized FIJI fixture,
        because the size limit applies here too — the two rules have to hold
        at once."""
        self.user.latitude, self.user.longitude = -17.5, -179.98
        self.user.save()
        response = self.trace(rings=[SMALL_CROSSING])
        self.assertEqual(response.status_code, 201)
        region = Region.objects.get(pk=response.json()['id'])
        self.assertTrue(region.crosses_antimeridian)
        self.assertTrue(region.contains(-17.5, -179.98))
        self.assertTrue(region.contains(-17.5, 179.98))
        self.assertFalse(region.contains(-17.5, 179.0))

    # ── Visibility ────────────────────────────────────────────────────────────
    def test_its_author_can_resolve_against_it_immediately(self):
        self.trace()
        with patch('regions.nominatim._get') as get:
            result = resolver.resolve(0.5, 0.5, kind='district', viewer=self.user)
        self.assertTrue(result.resolved)
        self.assertEqual(result.region.name, 'Msasani')
        get.assert_not_called()

    def test_nobody_else_sees_it_until_it_is_verified(self):
        self.trace()
        stranger = make_user('stranger')
        address = {'address': {'city': 'Dar', 'country_code': 'tz'}}
        with patch('regions.nominatim._get', side_effect=[address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district', viewer=stranger)
        self.assertFalse(result.resolved)

    def test_verifying_it_shares_it_with_everyone(self):
        self.trace()
        Region.objects.filter(source='user').update(verified=True)
        stranger = make_user('stranger')
        with patch('regions.nominatim._get') as get:
            result = resolver.resolve(0.5, 0.5, kind='district', viewer=stranger)
        self.assertTrue(result.resolved)
        get.assert_not_called()

    def test_an_unverified_outline_is_hidden_from_search(self):
        self.trace()
        stranger = make_user('stranger')
        self.client.force_authenticate(stranger)
        with patch('regions.nominatim.search', return_value=[]):
            body = self.client.get('/api/regions/search/?q=msasani').json()
        self.assertEqual(body, [])

    def test_an_unverified_outline_is_hidden_from_detail(self):
        pk = self.trace().json()['id']
        stranger = make_user('stranger')
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.get(f'/api/regions/{pk}/').status_code, 404)

    def test_a_traced_outline_scores_below_an_osm_one(self):
        """Both contain the point; the surveyed one wins. Tracing is a floor
        under coverage, not a way to override the map."""
        self.trace()
        make_region(
            osm_id=6500, name='Msasani', kind='district', rings=[SQUARE],
            admin_level=7, country_code='tz',
        )
        address = {'address': {'ward': 'Msasani', 'city': 'Dar', 'country_code': 'tz'}}
        with patch('regions.nominatim._get', side_effect=[address] + [[]] * 5):
            result = resolver.resolve(0.5, 0.5, kind='district', viewer=self.user)
        self.assertEqual(result.region.source, Region.Source.OSM)
