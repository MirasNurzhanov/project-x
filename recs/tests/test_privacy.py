"""The recommender must never need a coordinate.

This is the one property in the package that cannot be allowed to regress
quietly, and the reason is specific rather than general. `swifts/visibility.py`
gates location behind a viewer having genuinely watched a Swift. A recommender
that reads coordinates to rank would hold every coordinate in the database for
every candidate it considers — defeating that gate completely without touching
a line of it, and doing so somewhere nobody would think to look.

So these tests are structural. They do not check that ranking *happens* to work
without coordinates today; they check that it *cannot* start using them:

* no module under `recs/` mentions the column names at all,
* no model in `recs/` has a location-shaped field,
* the querysets the recommender builds do not load those columns,
* a Swift with a hidden location produces the same feature vector as one
  without.

The last of those matters most: if it did not hold, the *ordering* of the feed
would leak which Swifts carry a location, without a coordinate ever being read.
"""
import io
import pathlib
import tokenize

from swifts.models import SwiftVideo

from ..candidates import Candidate
from ..features import hydrate
from ..models import CreatorAffinity, SwiftImpression, SwiftSignal
from ..safety import FORBIDDEN_COLUMNS, permitted_ids
from ..scoring import estimate_heads
from ..testing import SwiftTestCase, make_swift, make_user, popular

RECS_DIR = pathlib.Path(__file__).resolve().parent.parent

#: Files that may name the columns: this test, and the module that documents
#: the ban. Everything else must not.
_ALLOWED = {'safety.py'}


def _code_tokens(path):
    """Every token in a file that is actually code.

    Comments and triple-quoted strings are skipped, because this package
    *documents* the coordinate ban at length and a naive substring scan flags
    its own prose — which is how the first version of this test failed. Ordinary
    quoted strings are kept: `values('latitude')` is a real way to read a column
    and has to be caught.
    """
    with io.open(path, encoding='utf-8') as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING and token.string.lstrip('rbfuRBFU')[:3] in ('"""', "'''"):
                continue
            yield token.string


class SourceTests(SwiftTestCase):
    def test_no_module_reads_a_coordinate_column(self):
        offenders = []
        for path in RECS_DIR.rglob('*.py'):
            if path.name in _ALLOWED or 'tests' in path.parts:
                continue
            code = ' '.join(_code_tokens(path))
            for column in FORBIDDEN_COLUMNS:
                if column in code:
                    offenders.append(f'{path.relative_to(RECS_DIR)}: {column}')
        self.assertEqual(offenders, [], f'coordinate columns referenced: {offenders}')

    def test_the_scanner_would_actually_catch_one(self):
        """A guard that cannot fail is not a guard.

        The scan skips docstrings, which is most of this package by volume, so
        it is worth proving it still sees code.
        """
        import tempfile
        import textwrap

        source = textwrap.dedent(
            '''
            """latitude in a docstring is fine."""

            def read(swift):
                return swift.latitude
            '''
        ).lstrip()

        with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8') as handle:
            handle.write(source)
            planted = pathlib.Path(handle.name)

        try:
            code = ' '.join(_code_tokens(planted))
            self.assertIn('latitude', code)
        finally:
            planted.unlink()

    def test_no_recs_model_stores_a_location(self):
        for model in (SwiftSignal, CreatorAffinity, SwiftImpression):
            names = {f.name for f in model._meta.get_fields()}
            for column in FORBIDDEN_COLUMNS:
                self.assertNotIn(column, names, f'{model.__name__}.{column}')
            # Also nothing that merely smells like one.
            for name in names:
                self.assertNotIn('lat', name.lower().split('_'))
                self.assertNotIn('lon', name.lower().split('_'))
                self.assertNotIn('geo', name.lower())

    def test_the_signal_endpoint_accepts_no_location_field(self):
        from ..serializers import SwiftSignalSerializer

        fields = set(SwiftSignalSerializer().get_fields())
        for column in FORBIDDEN_COLUMNS:
            self.assertNotIn(column, fields)
        self.assertNotIn('lat', fields)
        self.assertNotIn('lng', fields)


class QuerysetTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def test_the_safety_filter_returns_ids_and_nothing_else(self):
        swift = make_swift(self.author, located=True, shared=True)

        allowed = permitted_ids(self.viewer, [swift.id])

        self.assertEqual(allowed, {swift.id})
        # A set of integers. There is no row object here that could carry a
        # coordinate into the caller, which is the point of returning ids.
        self.assertTrue(all(isinstance(i, int) for i in allowed))

    def test_the_safety_filter_costs_the_same_for_one_candidate_or_fifty(self):
        """The invariant worth pinning is the shape, not the number.

        An exact query count here would be a test of `users.visibility`'s
        internals, which are free to change. That it does not grow with the
        candidate count is this module's own promise, and the one that breaks
        if somebody loops.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        one = [make_swift(self.author).id]
        many = [make_swift(self.author).id for _ in range(50)]

        with CaptureQueriesContext(connection) as small:
            permitted_ids(self.viewer, one)
        with CaptureQueriesContext(connection) as large:
            permitted_ids(self.viewer, many)

        self.assertEqual(len(small.captured_queries), len(large.captured_queries))

    def test_candidate_rows_do_not_load_coordinates(self):
        """`hydrate_rows` uses `.only()`, and the deferred set proves it.

        A Swift row fetched without its coordinate columns raises on access
        rather than returning them, which is a much stronger guarantee than
        "we do not read them".
        """
        from ..candidates import hydrate_rows

        swift = make_swift(self.author, located=True, shared=True)
        rows = hydrate_rows([Candidate(swift_id=swift.id, sources={'trending'})])
        row = rows[swift.id]

        deferred = row.get_deferred_fields()
        for column in FORBIDDEN_COLUMNS:
            self.assertIn(column, deferred, f'{column} was loaded into the ranker')


class RankingParityTests(SwiftTestCase):
    """A hidden location must not change where a Swift ranks."""

    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def _features_for(self, swifts):
        rows = {s.id: s for s in SwiftVideo.objects.filter(id__in=[x.id for x in swifts])}
        candidates = [Candidate(swift_id=s.id, sources={'trending'}) for s in swifts]
        return hydrate(self.viewer, candidates, rows)

    def test_located_and_plain_swifts_score_identically(self):
        from django.utils import timezone

        located = make_swift(self.author, located=True, shared=True)
        plain = make_swift(self.author)
        stamped = timezone.now() - timezone.timedelta(hours=3)
        SwiftVideo.objects.filter(id__in=[located.id, plain.id]).update(created_at=stamped)
        for s in (located, plain):
            popular(s, likes=7, views=70)

        feats = self._features_for([located, plain])

        self.assertEqual(
            estimate_heads(feats[located.id]),
            estimate_heads(feats[plain.id]),
        )

    def test_a_shared_location_does_not_promote_a_swift(self):
        """Sharing a location is not a ranking boost.

        Worth stating separately: it would be an easy and superficially
        reasonable feature — located Swifts are more interesting! — and it
        would turn the feed into a probe for which of your friends geotag.
        """
        from django.utils import timezone

        shared = make_swift(self.author, located=True, shared=True)
        private = make_swift(self.author, located=True, shared=False)
        stamped = timezone.now() - timezone.timedelta(hours=3)
        SwiftVideo.objects.filter(id__in=[shared.id, private.id]).update(created_at=stamped)
        for s in (shared, private):
            popular(s, likes=7, views=70)

        feats = self._features_for([shared, private])

        self.assertEqual(
            estimate_heads(feats[shared.id]),
            estimate_heads(feats[private.id]),
        )


class SignalTests(SwiftTestCase):
    def test_a_location_reveal_signal_carries_no_location(self):
        """The reveal is an interaction and that is all it records."""
        from ..ingest import record

        viewer = make_user('viewer')
        author = make_user('author')
        swift = make_swift(author, located=True, shared=True)

        signal = record(viewer, SwiftSignal.Kind.LOCATION_REVEAL, swift=swift)

        stored = SwiftSignal.objects.filter(pk=signal.pk).values().get()
        for key, value in stored.items():
            self.assertNotIn('lat', key.lower())
            self.assertNotIn('lon', key.lower())
        # And it did move affinity — it is a real, useful signal, just not a
        # geographic one.
        self.assertTrue(
            CreatorAffinity.objects.filter(viewer=viewer, creator=author, score__gt=0).exists()
        )
