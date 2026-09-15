"""Deciding what size a Nominatim result is, and saying so when we cannot.

The district lookup's real failure mode is invisible from outside: someone taps
"Use my district", gets "nothing mapped", and only the `addresstype` on the
discarded result distinguishes "OSM has no polygon here" from "we do not know
this country's word for a district". These pin both the words we accept and the
logging that is supposed to reveal the ones we do not.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from . import nominatim, resolver
from .models import Region
from .tests import SQUARE


def polygon(name, addresstype, osm_id, ring=None, **extra):
    payload = {
        'osm_type': 'relation', 'osm_id': osm_id, 'name': name,
        'display_name': f'{name}, Somewhere', 'addresstype': addresstype,
        'category': 'boundary',
        'geojson': {'type': 'Polygon', 'coordinates': [ring or SQUARE]},
        'address': {'country_code': 'gb'},
    }
    payload.update(extra)
    return payload


# ─── Words ────────────────────────────────────────────────────────────────────
class DistrictTypeTests(SimpleTestCase):
    def test_ward_is_a_district(self):
        """A ward is the primary city subdivision in Tanzania, Kenya, Vietnam
        and much of Japan, and a subdivision of a borough in the UK. Either way
        it is a district, and it used to be discarded."""
        self.assertEqual(nominatim._classify({'addresstype': 'ward'}), 'district')

    def test_ward_is_an_address_key_for_districts(self):
        """Recognising the word is only half of it: the reverse lookup reads a
        *name* out of the address hierarchy, and without the key there it never
        gets as far as classifying anything."""
        self.assertIn('ward', nominatim.ADDRESS_KEYS['district'])

    def test_ward_does_not_displace_a_larger_district(self):
        """Address keys are tried most-specific-first and the first present
        wins. A UK point carries both, and a borough is what somebody means by
        "my district" — a ward is a few thousand people."""
        name = nominatim._address_name(
            {'borough': 'Camden', 'ward': 'Holborn and Covent Garden'}, 'district',
        )
        self.assertEqual(name, 'Camden')

    def test_ward_is_used_when_it_is_all_there_is(self):
        name = nominatim._address_name({'ward': 'Msasani', 'city': 'Dar es Salaam'}, 'district')
        self.assertEqual(name, 'Msasani')

    def test_residential_is_not_a_district_type(self):
        """It was dead configuration: `residential` reaches Nominatim as
        `landuse=residential`, and ALLOWED_CLASSES rejects the whole result
        before classification ever runs. Keeping the word implied a behaviour
        that could not happen."""
        self.assertNotIn('residential', nominatim.DISTRICT_TYPES)

    def test_a_residential_landuse_polygon_is_rejected_outright(self):
        """The reason removing the word changed nothing: this never got past
        the class filter. A planning polygon is not a place anyone lives in the
        sense of "my district"."""
        self.assertFalse(nominatim._is_area({'class': 'landuse', 'type': 'residential'}))

    def test_other_landuse_is_rejected_too(self):
        for landuse in ('industrial', 'retail', 'forest', 'farmland'):
            self.assertFalse(
                nominatim._is_area({'class': 'landuse', 'type': landuse}), landuse,
            )

    def test_real_areas_are_still_accepted(self):
        self.assertTrue(nominatim._is_area({'class': 'boundary', 'type': 'administrative'}))
        self.assertTrue(nominatim._is_area({'class': 'place', 'type': 'suburb'}))
        self.assertTrue(nominatim._is_area({'category': 'boundary'}))


# ─── administrative ───────────────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class AdministrativeHintTests(TestCase):
    """`administrative` is Nominatim's answer for a boundary relation it could
    not place at a named level. It covers everything from a parish to a country,
    so it is never a size on its own.

    It is accepted only where three independent facts agree: the address
    hierarchy named this place at the level we asked for, for the point we are
    standing on; the boundary carries exactly that name; and the boundary
    contains that point. None of those is a guess about admin_level, which is
    the guess that broke this feature before.
    """

    def setUp(self):
        cache.clear()

    ADDRESS = {
        'address': {
            'ward': 'Msasani',
            'city': 'Dar es Salaam',
            'country_code': 'tz',
        },
    }

    def test_alone_it_is_never_a_size(self):
        self.assertEqual(nominatim._classify({'addresstype': 'administrative'}), 'other')

    def test_it_is_accepted_when_the_hierarchy_and_the_point_agree(self):
        responses = [
            self.ADDRESS,
            [polygon('Msasani', 'administrative', 3001)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNotNone(result.region)
        self.assertEqual(result.region.name, 'Msasani')
        self.assertEqual(result.region.kind, 'district')

    def test_a_different_name_is_refused(self):
        """The relation contains the point and is administrative, but the
        hierarchy did not name *it* — it named something else. Without the name
        match this is exactly how a province gets labelled a district."""
        responses = [
            self.ADDRESS,
            [polygon('Kinondoni', 'administrative', 3002)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assertEqual(Region.objects.get(osm_id=3002).kind, 'other')

    def test_a_boundary_that_does_not_contain_the_point_is_refused(self):
        """And, critically, is not relabelled in the cache on the way past. A
        wrong kind in this table is permanent: the cache answers before
        upstream is ever asked again."""
        far = [[50, 50], [51, 50], [51, 51], [50, 51], [50, 50]]
        responses = [
            self.ADDRESS,
            [polygon('Msasani', 'administrative', 3003, ring=far)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assertEqual(Region.objects.get(osm_id=3003).kind, 'other')

    def test_a_country_can_never_be_promoted(self):
        """It says what it is, so it is not inconclusive, so the hint never
        applies to it — however well the name happens to match."""
        world = [[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]]
        responses = [
            self.ADDRESS,
            [polygon('Msasani', 'country', 3004, ring=world,
                     extratags={'admin_level': '2'})],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assertEqual(Region.objects.get(osm_id=3004).kind, 'other')

    def test_a_state_can_never_be_promoted(self):
        big = [[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]]
        responses = [
            self.ADDRESS,
            [polygon('Msasani', 'state', 3005, ring=big)],
        ]
        with patch('regions.nominatim._get', side_effect=responses):
            result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)

    def test_an_explicit_label_still_wins_over_the_hint(self):
        """The hint fills a blank; it never overrides an answer. A relation
        labelled a city is a city even when we asked about districts."""
        self.assertFalse(
            nominatim._hint_applies({'addresstype': 'city', 'name': 'Msasani'}, 'Msasani'),
        )

    def test_the_hint_is_case_and_space_insensitive(self):
        self.assertTrue(
            nominatim._hint_applies(
                {'addresstype': 'administrative', 'name': ' msasani '}, 'Msasani',
            ),
        )

    def test_no_hint_means_no_promotion(self):
        self.assertFalse(
            nominatim._hint_applies({'addresstype': 'administrative', 'name': 'X'}, None),
        )


# ─── Logging ──────────────────────────────────────────────────────────────────
@override_settings(OVERPASS_ENABLED=False)
class RejectionLoggingTests(TestCase):
    """Every discarded result has to say enough to identify the pattern.

    Aggregating these by `addresstype` is how the type lists get extended with
    evidence instead of guesswork — which is the whole reason `administrative`
    is not simply added to DISTRICT_TYPES.
    """

    def setUp(self):
        cache.clear()

    def assert_logged(self, logs, reason, **expected):
        line = next((record for record in logs.records if reason in record.getMessage()), None)
        self.assertIsNotNone(line, f'no {reason!r} line in {[r.getMessage() for r in logs.records]}')
        fields = line.nominatim
        for key, value in expected.items():
            self.assertEqual(fields.get(key), value, f'{key} in {fields}')
        return fields

    def test_an_unknown_addresstype_is_logged_with_everything_needed(self):
        with self.assertLogs('regions.nominatim', level='INFO') as logs:
            nominatim._region_from_payload(polygon(
                'Somewhere', 'parish', 3100,
                extratags={'admin_level': '10'},
            ))
        fields = self.assert_logged(
            logs, 'unclassifiable-addresstype',
            addresstype='parish', osm_type='relation',
            klass='boundary', admin_level='10',
        )
        for private in ('name', 'display_name', 'osm_id', 'query'):
            self.assertNotIn(private, fields)
        self.assertEqual(fields['country_code'], 'gb')

    def test_a_wrong_class_is_logged_with_its_class_and_type(self):
        with self.assertLogs('regions.nominatim', level='INFO') as logs:
            with patch('regions.nominatim._fetch', return_value=[
                {'class': 'landuse', 'type': 'residential', 'osm_type': 'way',
                 'osm_id': 3101, 'name': 'Estate', 'display_name': 'Estate'},
            ]):
                nominatim.search('estate')
        self.assert_logged(
            logs, 'class-is-not-an-area',
            klass='landuse', type='residential',
        )

    def test_a_place_with_no_polygon_is_logged(self):
        with self.assertLogs('regions.nominatim', level='INFO') as logs:
            nominatim._region_from_payload({
                'osm_type': 'node', 'osm_id': 3102, 'name': 'Pin',
                'display_name': 'Pin', 'addresstype': 'village',
                'geojson': {'type': 'Point', 'coordinates': [1, 2]},
            })
        self.assert_logged(logs, 'no-mapped-boundary', addresstype='village')

    def test_a_wrong_size_candidate_is_logged_with_both_sizes(self):
        """The district lookup found a boundary of that name containing the
        point — and it is the city. Worth a line, because a country where this
        happens constantly is a country whose district level we are reading
        from the wrong address key."""
        address = {
            'address': {
                'suburb': 'Old Town', 'city': 'Metropolis', 'country_code': 'gb',
            },
        }
        with self.assertLogs('regions.nominatim', level='INFO') as logs:
            with patch('regions.nominatim._get', side_effect=[
                address, [polygon('Old Town', 'city', 3103)],
            ]):
                result = resolver.resolve(0.5, 0.5, kind='district')
        self.assertIsNone(result.region)
        self.assert_logged(logs, 'wrong-size-for-this-lookup', wanted='district', got='city')

    def test_a_level_the_hierarchy_cannot_name_is_logged(self):
        """The commonest district miss of all, and the one that says nothing
        about addresstype — because there was no result to have one.

        Recorded by the resolver rather than the parser, because the fact is
        about the *pipeline*: stage 2 produced no name for stage 3 to search
        for, leaving only the boundary-relation fallback. Aggregating it by
        country separates "OSM has no boundary here" from "the hierarchy does
        not name this level here", which are different problems."""
        address = {'address': {'city': 'Flatville', 'country_code': 'gb'}}
        with self.assertLogs('regions.upstream', level='INFO') as logs:
            with patch('regions.nominatim._get', side_effect=[address]):
                resolver.resolve(0.5, 0.5, kind='district')
        line = next(
            r for r in logs.records if 'no_address_name_at_level' in r.getMessage()
        )
        self.assertEqual(line.upstream['kind'], 'district')
        self.assertIn('city', line.upstream['address_keys'])

    def test_a_promotion_is_logged_so_it_can_be_audited(self):
        address = {'address': {'ward': 'Msasani', 'city': 'Dar', 'country_code': 'tz'}}
        with self.assertLogs('regions.nominatim', level='INFO') as logs:
            with patch('regions.nominatim._get', side_effect=[
                address, [polygon('Msasani', 'administrative', 3104)],
            ]):
                resolver.resolve(0.5, 0.5, kind='district')
        line = next(
            r for r in logs.records
            if 'classified_by_address_hint' in r.getMessage()
        )
        self.assertEqual(line.nominatim['addresstype'], 'administrative')
        self.assertEqual(line.nominatim['kind'], 'district')
