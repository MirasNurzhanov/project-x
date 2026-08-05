"""`core.jsonio` must be indistinguishable from what it replaced.

A renderer swap is invisible right up until it is not, and the failure lands on
a phone that has already shipped. So both encoders are run over the same input
and compared as bytes -- not "close enough", not "parses to the same thing",
the same bytes -- across every type the API actually emits and the real
payloads of the hot endpoints.
"""
import datetime
import decimal
import ipaddress
import uuid

from django.test import SimpleTestCase
from django.utils.functional import lazy
from django.utils.safestring import mark_safe
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from core import perf_probe
from core.jsonio import ORJSONParser, ORJSONRenderer

# Imported as a module, not `from ... import Probe`: a TestCase bound at module
# level here would be collected and run a second time by every invocation of
# this module.

lazy_str = lazy(lambda s: s, str)


class Equivalence(SimpleTestCase):
    """Type by type, against DRF's own renderer."""

    def assert_same(self, value, label=None):
        expected = JSONRenderer().render(value)
        actual = ORJSONRenderer().render(value)
        self.assertEqual(actual, expected, label or repr(value))
        return actual

    def test_scalars_and_containers(self):
        for value in (
            None, True, False, 0, -1, 3.5, '', 'plain',
            [], {}, [1, 2, 3], {'a': 1}, {'a': {'b': [1, {'c': None}]}},
            (1, 2),
        ):
            self.assert_same(value)

    def test_decimal_renders_as_a_number(self):
        # The one that would break the map: latitude and longitude arrive at
        # the renderer as raw Decimal, and the client reads them as numbers.
        out = self.assert_same({'latitude': decimal.Decimal('25.20000000')})
        self.assertEqual(out, b'{"latitude":25.2}')

    def test_datetimes(self):
        utc = datetime.timezone.utc
        offset = datetime.timezone(datetime.timedelta(hours=4))
        for value in (
            datetime.datetime(2026, 8, 5, 12, 0, tzinfo=utc),
            datetime.datetime(2026, 8, 5, 12, 0, 30, 123456, tzinfo=utc),
            datetime.datetime(2026, 8, 5, 12, 0, tzinfo=offset),
            datetime.datetime(2026, 8, 5, 12, 0),
            datetime.date(1995, 1, 1),
            datetime.time(9, 30),
            datetime.timedelta(seconds=90),
        ):
            self.assert_same({'v': value})

    def test_utc_keeps_the_z_suffix(self):
        # DRF rewrites `+00:00` to `Z`; orjson only does that when asked.
        out = ORJSONRenderer().render(
            {'at': datetime.datetime(2026, 8, 5, 12, 0, tzinfo=datetime.timezone.utc)}
        )
        self.assertEqual(out, b'{"at":"2026-08-05T12:00:00Z"}')

    def test_types_that_reach_drfs_encoder(self):
        for value in (
            uuid.uuid4(),
            lazy_str('translated'),
            mark_safe('<b>safe</b>'),
            b'bytes',
            ipaddress.IPv4Address('10.0.0.1'),
            ipaddress.IPv6Network('2001:db8::/32'),
        ):
            self.assert_same({'v': value})

    def test_non_string_keys_are_coerced_not_rejected(self):
        out = self.assert_same({1: 'a', 2.5: 'b', None: 'c'})
        self.assertEqual(out, b'{"1":"a","2.5":"b","null":"c"}')

    def test_javascript_line_terminators_are_escaped(self):
        # Valid inside a JSON string, fatal inside a JavaScript one.
        bio = 'first' + chr(0x2028) + 'second' + chr(0x2029) + 'third'
        out = self.assert_same({'bio': bio})
        self.assertEqual(out, b'{"bio":"first\\u2028second\\u2029third"}')
        self.assertNotIn(b'\xe2\x80\xa8', out)
        self.assertNotIn(b'\xe2\x80\xa9', out)

    def test_non_ascii_is_not_escaped(self):
        # DRF sets ensure_ascii=False, so this is a match, not a change -- and
        # it is most of the payload for users writing Arabic or Cyrillic.
        out = self.assert_same({'name': 'Нурдаулет', 'city': 'دبي'})
        self.assertIn('Нурдаулет'.encode(), out)

    def test_none_renders_empty(self):
        self.assertEqual(ORJSONRenderer().render(None), b'')

    def test_indent_falls_back_to_the_stdlib_encoder(self):
        data = {'b': 1, 'a': [1, 2]}
        for ctx in ({'indent': 4}, {'indent': 2}):
            self.assertEqual(
                ORJSONRenderer().render(data, None, ctx),
                JSONRenderer().render(data, None, ctx),
            )

    def test_parser_round_trip(self):
        import io

        parsed = ORJSONParser().parse(io.BytesIO(b'{"a":[1,2],"b":"\xd8\xaf"}'))
        self.assertEqual(parsed, {'a': [1, 2], 'b': 'د'})

    def test_parser_reports_bad_json_as_a_client_error(self):
        import io

        from rest_framework.exceptions import ParseError

        with self.assertRaises(ParseError):
            ORJSONParser().parse(io.BytesIO(b'{"a":'))


class RealPayloads(perf_probe.Probe):
    """The endpoints themselves, byte for byte.

    Inherits `Probe` only for its fixture -- 30 nearby users with interests,
    zones, private accounts and Decimal coordinates is a wider spread of types
    than a hand-written case would be.
    """

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.me)

    def test_probe(self):
        # Inherited from Probe; not what this class is for.
        self.skipTest('measurement, not assertion')

    def test_endpoints_render_identically(self):
        for path in (
            '/api/proximity/nearby/',
            '/api/clubs/',
            '/api/clubs/?joined=true',
            '/api/stories/nearby/',
            '/api/friends/list/',
            '/api/notifications/',
            '/api/users/search/?q=user',
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                data = response.data
                self.assertEqual(
                    ORJSONRenderer().render(data),
                    JSONRenderer().render(data),
                )
