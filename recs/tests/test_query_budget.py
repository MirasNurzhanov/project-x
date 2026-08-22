"""The recommender must not get slower as the candidate pool grows.

Every stage here is written to be bulk, and "bulk" is the kind of property that
degrades silently: one `.author.username` inside a loop turns a fixed-cost
request into an N+1 that only shows up under real data. `core/test_query_budgets.py`
guards the rest of the app the same way and for the same reason.

The budgets are ceilings, not equalities. An exact count would fail whenever
`users.visibility` changed something internal, which is not this package's
business — what *is* this package's business is that the count does not depend
on how many candidates there are.
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from ..candidates import Candidate, gather, hydrate_rows
from ..features import hydrate
from ..pipeline import build_session, page
from ..testing import (
    SwiftTestCase, bulk_swifts, engage, make_swift, make_user, popular,
)


def _queries(fn):
    with CaptureQueriesContext(connection) as captured:
        fn()
    return len(captured.captured_queries)


class HydrationTests(SwiftTestCase):
    """The stage with the tightest promise: seven queries, any pool size."""

    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def _hydrate_n(self, n):
        swifts = bulk_swifts(self.author, n)
        rows = hydrate_rows([Candidate(swift_id=s.id, sources={'trending'}) for s in swifts])
        candidates = [Candidate(swift_id=s.id, sources={'trending'}) for s in swifts]
        return lambda: hydrate(self.viewer, candidates, rows)

    def test_hydration_is_a_fixed_number_of_queries(self):
        small = _queries(self._hydrate_n(3))

        # Ten statements for seven lookups: the friend-graph lookup is itself
        # three queries inside `users.visibility`, and this test calls
        # `hydrate` directly rather than through the pipeline, which passes
        # that set in and skips them. Measured, not guessed — an aspirational
        # number here would just be a comment that disagrees with the code.
        self.assertLessEqual(small, 10, f'hydration cost {small} queries')

    def test_hydration_does_not_grow_with_the_pool(self):
        three = _queries(self._hydrate_n(3))
        forty = _queries(self._hydrate_n(40))

        self.assertEqual(three, forty)

    def test_fetching_candidate_rows_is_one_query(self):
        swifts = bulk_swifts(self.author, 25)
        candidates = [Candidate(swift_id=s.id, sources={'trending'}) for s in swifts]

        self.assertEqual(_queries(lambda: hydrate_rows(candidates)), 1)


class GenerationTests(SwiftTestCase):
    def test_sourcing_does_not_grow_with_the_number_of_swifts(self):
        viewer = make_user('viewer')
        author = make_user('author')
        bulk_swifts(author, 5)
        few = _queries(lambda: gather(viewer))

        bulk_swifts(author, 60)
        many = _queries(lambda: gather(viewer))

        self.assertEqual(few, many)

    def test_sourcing_does_not_grow_with_the_number_of_creators(self):
        viewer = make_user('viewer')
        for i in range(3):
            make_swift(make_user(f'a{i}'))
        few = _queries(lambda: gather(viewer))

        for i in range(30):
            make_swift(make_user(f'b{i}'))
        many = _queries(lambda: gather(viewer))

        self.assertEqual(few, many)


class WholePipelineTests(SwiftTestCase):
    """The number that actually matters: one ranked page."""

    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        for i in range(6):
            author = make_user(f'creator{i}')
            for swift in bulk_swifts(author, 8):
                popular(swift, likes=5, views=40)

    def test_a_cold_ranked_page_stays_within_budget(self):
        count = _queries(lambda: page(self.viewer, limit=10))

        # ~41 at the time of writing: six candidate sources at one to three
        # statements each, the visibility and exposure filters, the row fetch,
        # hydration, and the impression upsert. Every one of them is bounded,
        # which the sibling tests prove; this ceiling exists to catch a
        # regression *in kind* — a loop appearing somewhere — rather than to
        # police a refactor that moves it by one.
        #
        # It is also paid once per session, not once per swipe. A continued
        # page is a cache read, which `test_a_warm_page_is_much_cheaper` pins.
        self.assertLess(count, 50, f'a cold ranked page cost {count} queries')

    def test_a_warm_page_is_much_cheaper_than_a_cold_one(self):
        """The whole reason sessions exist."""
        first = page(self.viewer, limit=5)

        warm = _queries(lambda: page(self.viewer, cursor=first.cursor, limit=5))
        cold = _queries(lambda: page(self.viewer, limit=5))

        self.assertLess(warm, cold / 2)

    def test_the_pipeline_does_not_grow_with_candidate_count(self):
        small = _queries(lambda: build_session(self.viewer))

        for i in range(10):
            author = make_user(f'extra{i}')
            for swift in bulk_swifts(author, 10):
                popular(swift, likes=5, views=40)

        large = _queries(lambda: build_session(self.viewer))

        self.assertEqual(small, large)

    def test_an_active_viewer_costs_no_more_than_a_new_one(self):
        """Affinity and collaborative sourcing add queries, not query *loops*."""
        cold = _queries(lambda: build_session(self.viewer))

        for swift in bulk_swifts(make_user('favourite'), 3):
            engage(self.viewer, swift, times=2)

        warm = _queries(lambda: build_session(self.viewer))

        # Two extra sources, each a bounded number of statements.
        self.assertLess(warm - cold, 10)
