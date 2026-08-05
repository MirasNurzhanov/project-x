"""orjson-backed JSON for DRF, byte-for-byte compatible with what it replaces.

The map poll is the reason: every client asks for ~25KB of JSON every ten
seconds, and the stdlib encoder builds that one Python object at a time. orjson
does it in Rust.

The whole point of this module is that nothing on the wire changes. A phone
already in someone's pocket parses what this emits, so every place the two
encoders could disagree is pinned to DRF's answer rather than orjson's default:

* ``OPT_UTC_Z`` reproduces DRF's ``+00:00`` -> ``Z`` rewrite for UTC datetimes.
* ``OPT_NON_STR_KEYS`` reproduces the stdlib's coercion of non-string dict
  keys, which orjson would otherwise reject outright.
* Everything orjson has no opinion on -- ``Decimal``, ``timedelta``, lazy
  translation strings, querysets -- is handed to DRF's own encoder, so the
  answer is DRF's by construction rather than a re-implementation that can
  drift. ``Decimal`` matters most: latitude and longitude reach the renderer as
  raw ``Decimal`` and DRF renders them as JSON numbers, not strings.
* The U+2028 / U+2029 escape is kept. They are line terminators in JavaScript
  but not in JSON, so an unescaped one in a bio would break any client that
  evaluates rather than parses the body.

`core.test_json` asserts that equivalence against real endpoint payloads rather
than trusting this list.

Named `jsonio` rather than `json` so nothing in this package can shadow the
stdlib module for a reader, whatever Python's import rules say.
"""
import orjson
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder

#: DRF's encoder, reused rather than re-implemented -- see the module docstring.
#: Bound once because it holds no per-call state.
_drf_default = JSONEncoder().default

_OPTIONS = orjson.OPT_UTC_Z | orjson.OPT_NON_STR_KEYS

# U+2028 (line separator) and U+2029 (paragraph separator), UTF-8 encoded.
# Written as byte escapes rather than as the characters themselves: they *are*
# line breaks to a great deal of tooling, so a source file containing them
# reads differently depending on what is opening it.
#
# Matched against the encoded bytes, so the scan is a memchr over the buffer
# rather than a decode-and-rescan. Both are almost always no-ops, which is the
# case bytes.replace is fastest at.
_LINE_SEP = b'\xe2\x80\xa8'
_PARA_SEP = b'\xe2\x80\xa9'


class ORJSONRenderer(JSONRenderer):
    """Drop-in for `rest_framework.renderers.JSONRenderer`."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b''

        renderer_context = renderer_context or {}
        if self.get_indent(accepted_media_type, renderer_context) is not None:
            # Pretty-printing is the browsable API and `?indent=`, neither of
            # which is a hot path. orjson only offers two-space indentation, so
            # rather than quietly change what those emit, they keep the stdlib
            # encoder -- the one place where being identical is worth more than
            # being fast.
            return super().render(data, accepted_media_type, renderer_context)

        ret = orjson.dumps(data, default=_drf_default, option=_OPTIONS)
        return ret.replace(_LINE_SEP, b'\\u2028').replace(_PARA_SEP, b'\\u2029')


class ORJSONParser(JSONParser):
    """Drop-in for `rest_framework.parsers.JSONParser`.

    Request bodies here are small -- a location report is a hundred bytes -- so
    this is not where the time is. It exists so one library owns both
    directions, and so a future endpoint that accepts something large gets the
    fast path without anyone having to remember to ask for it.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        try:
            # orjson decodes UTF-8 itself and rejects NaN/Infinity, which is
            # what DRF's default `strict` setting asks the stdlib for anyway.
            return orjson.loads(stream.read())
        except (orjson.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ParseError(f'JSON parse error - {exc}')
