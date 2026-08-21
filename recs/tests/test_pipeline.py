"""The recommender, end to end.

One scenario per behaviour the brief names. They are written as "given this
history, this Swift ranks above that one" rather than "the score is 4.7",
because the scores are tuning parameters and the *orderings* are the contract —
a suite that asserts on numbers has to be rewritten every time a weight moves,
and gets rewritten to match whatever the code now does, which is how a ranking
suite stops catching anything.
"""
from django.test import override_settings
from django.utils import timezone

from swifts.models import SwiftVideo

from ..models import CreatorAffinity, SwiftImpression
from ..params import load_params
from ..pipeline import build_session, page
from ..testing import (
    SwiftTestCase, age_swift, befriend, block, bulk_swifts, engage,
    make_swift, make_user, popular, skip, watch,
)


class ColdStartTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('newcomer')

    def test_a_viewer_with_no_history_still_gets_a_feed(self):
        author = make_user('somebody')
        for _ in range(5):
            popular(make_swift(author), likes=4, views=40)

        ids, _, stages, _ = build_session(self.viewer)

        self.assertGreater(len(ids), 0)
        self.assertGreater(stages['generated'], 0)

    def test_cold_start_does_not_run_the_history_sources(self):
        """A viewer with no affinities cannot have collaborative candidates.

        Asserted because the alternative is two hops over an empty table on
        every request from every new user, which is exactly the population
        least tolerant of a slow first feed.
        """
        author = make_user('somebody')
        make_swift(author)

        _, _, _, source_counts = build_session(self.viewer)

        self.assertNotIn('collaborative', source_counts)
        self.assertNotIn('affinity', source_counts)
        self.assertIn('cold_start', source_counts)

    def test_leaving_cold_start(self):
        author = make_user('somebody')
        swift = make_swift(author)
        engage(self.viewer, swift)

        _, _, _, source_counts = build_session(self.viewer)

        self.assertNotIn('cold_start', source_counts)


class AffinityTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.loved = make_user('loved')
        self.ignored = make_user('ignored')

    def test_a_strongly_liked_creator_outranks_a_stranger(self):
        history = make_swift(self.loved)
        engage(self.viewer, history, times=6)

        theirs = make_swift(self.loved)
        strangers = make_swift(self.ignored)
        for s in (theirs, strangers):
            popular(s, likes=5, views=50)
            age_swift(s, hours=1)

        ids, _, _, _ = build_session(self.viewer)

        self.assertIn(theirs.id, ids)
        self.assertIn(strangers.id, ids)
        self.assertLess(ids.index(theirs.id), ids.index(strangers.id))

    def test_a_repeatedly_skipped_creator_is_pushed_down(self):
        history = make_swift(self.ignored)
        skip(self.viewer, history, times=5)

        skipped = popular(make_swift(self.ignored), likes=20, views=40)
        neutral = popular(make_swift(self.loved), likes=5, views=40)
        age_swift(skipped, hours=1)
        age_swift(neutral, hours=1)

        ids, _, _, _ = build_session(self.viewer)

        self.assertIn(neutral.id, ids)
        if skipped.id in ids:
            self.assertLess(ids.index(neutral.id), ids.index(skipped.id))

    def test_not_interested_removes_a_creator_entirely(self):
        """A filter, not a penalty. Somebody who said no should get zero."""
        history = make_swift(self.ignored)
        engage(self.viewer, history)
        CreatorAffinity.objects.filter(
            viewer=self.viewer, creator=self.ignored,
        ).update(suppressed=True)

        theirs = popular(make_swift(self.ignored), likes=500, views=600)
        other = make_swift(self.loved)

        ids, _, stages, _ = build_session(self.viewer)

        self.assertNotIn(theirs.id, ids)
        self.assertIn(other.id, ids)
        self.assertGreaterEqual(stages['filtered_suppressed'], 1)


class FriendTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.friend = make_user('friend')
        self.stranger = make_user('stranger')
        befriend(self.viewer, self.friend)

    def test_a_friends_swift_is_sourced_as_in_network(self):
        make_swift(self.friend)
        _, _, _, source_counts = build_session(self.viewer)
        self.assertGreaterEqual(source_counts.get('friends', 0), 1)

    def test_friends_cannot_take_the_whole_session(self):
        """The ceiling that stops the feed becoming a friends-only timeline."""
        bulk_swifts(self.friend, 40)
        bulk_swifts(self.stranger, 20)
        for swift in SwiftVideo.objects.filter(author=self.stranger):
            popular(swift, likes=3, views=30)

        ids, sources, _, _ = build_session(self.viewer)

        friend_slots = sum(1 for i in ids if sources.get(i) == 'friends')
        ceiling = int(load_params().limits.session_size
                      * load_params().quotas.friends_ceiling_pct)
        self.assertLessEqual(friend_slots, ceiling)
        self.assertLess(friend_slots, len(ids))


class SafetyTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def test_a_blocked_creators_swifts_never_appear(self):
        swift = popular(make_swift(self.author), likes=100, views=200)
        block(self.viewer, self.author)

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_blocking_in_the_other_direction_also_hides(self):
        swift = make_swift(self.author)
        block(self.author, self.viewer)

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_a_deleted_swift_never_appears(self):
        swift = make_swift(self.author)
        SwiftVideo.objects.filter(pk=swift.pk).update(deleted_at=timezone.now())

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_a_removed_swift_never_appears(self):
        swift = make_swift(self.author)
        SwiftVideo.objects.filter(pk=swift.pk).update(
            moderation=SwiftVideo.Moderation.REMOVED,
        )

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_a_private_swift_never_appears(self):
        swift = make_swift(self.author, visibility=SwiftVideo.Visibility.PRIVATE)

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_a_friends_only_swift_from_a_stranger_never_appears(self):
        swift = make_swift(self.author, visibility=SwiftVideo.Visibility.FRIENDS)

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)

    def test_your_own_swifts_are_never_recommended_to_you(self):
        mine = make_swift(self.viewer)

        ids, _, _, _ = build_session(self.viewer)

        self.assertNotIn(mine.id, ids)

    def test_a_location_hidden_swift_ranks_exactly_like_any_other(self):
        """The property the whole location design rests on.

        Two Swifts identical but for a hidden coordinate must produce the same
        ranking outcome — if they did not, the *ordering* would be a channel
        leaking which Swifts have locations, without a coordinate ever being
        read. Same author, same age, same counters, so any difference in
        position could only come from the location columns.
        """
        located = make_swift(self.author, located=True, shared=False)
        plain = make_swift(self.author)
        # One timestamp for both, not two calls to `age_swift`: the heads
        # depend on age, so ageing them a few microseconds apart would make
        # them differ for a reason that has nothing to do with location.
        stamped = timezone.now() - timezone.timedelta(hours=2)
        SwiftVideo.objects.filter(id__in=[located.id, plain.id]).update(created_at=stamped)
        for s in (located, plain):
            popular(s, likes=10, views=100)
            s.refresh_from_db()

        from ..candidates import Candidate
        from ..features import hydrate
        from ..scoring import estimate_heads

        rows = {s.id: s for s in SwiftVideo.objects.filter(id__in=[located.id, plain.id])}
        candidates = [Candidate(swift_id=located.id, sources={'trending'}),
                      Candidate(swift_id=plain.id, sources={'trending'})]
        feats = hydrate(self.viewer, candidates, rows)

        self.assertEqual(
            estimate_heads(feats[located.id]),
            estimate_heads(feats[plain.id]),
        )


class ExposureTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.author = make_user('author')

    def test_an_already_watched_swift_is_not_re_served(self):
        swift = make_swift(self.author)
        watch(self.viewer, swift)
        from swifts.models import SwiftView
        SwiftView.objects.create(swift=swift, viewer=self.viewer)

        ids, _, stages, _ = build_session(self.viewer)

        self.assertNotIn(swift.id, ids)
        self.assertGreaterEqual(stages['filtered_exposure'], 1)

    def test_serving_a_page_records_impressions(self):
        bulk_swifts(self.author, 3)

        result = page(self.viewer, limit=3)

        self.assertEqual(
            SwiftImpression.objects.filter(viewer=self.viewer).count(),
            len(result.swift_ids),
        )

    def test_a_served_swift_does_not_come_back_in_the_next_session(self):
        bulk_swifts(self.author, 4)
        first = page(self.viewer, limit=2)

        # A brand new session, not a continuation.
        second = build_session(self.viewer)[0]

        for swift_id in first.swift_ids:
            self.assertNotIn(swift_id, second)

    def test_a_served_but_unwatched_swift_returns_after_the_reserve_window(self):
        """Served is not seen. Over-fetching must not burn candidates."""
        bulk_swifts(self.author, 3)
        served = page(self.viewer, limit=3).swift_ids
        self.assertTrue(served)

        SwiftImpression.objects.filter(viewer=self.viewer).update(
            last_served_at=timezone.now() - timezone.timedelta(hours=1000),
        )

        ids, _, _, _ = build_session(self.viewer)
        self.assertTrue(set(served) & set(ids))

    def test_no_duplicates_within_a_session(self):
        author_b = make_user('author_b')
        befriend(self.viewer, self.author)
        for swift in bulk_swifts(self.author, 6) + bulk_swifts(author_b, 6):
            popular(swift, likes=5, views=20)

        ids, _, _, _ = build_session(self.viewer)

        self.assertEqual(len(ids), len(set(ids)))


class DiversityTests(SwiftTestCase):
    """The caps, and the one case where they deliberately give way.

    `max_per_creator_per_page` is per *page*, not per session, and it holds
    only while the candidate pool can satisfy it. Both halves are tested,
    because the second is the surprising one: a cap that truncated the feed
    rather than yielding would be a worse product and a silent failure.
    """

    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        self.rules = load_params().diversity
        self.window = load_params().limits.page_size

    def _authors_in_order(self, ids):
        by_id = dict(SwiftVideo.objects.filter(id__in=ids).values_list('id', 'author_id'))
        return [by_id.get(i) for i in ids]

    def test_one_creator_cannot_fill_a_page(self):
        prolific = make_user('prolific')
        for swift in bulk_swifts(prolific, 30):
            popular(swift, likes=50, views=100)
        # Enough other creators, with enough Swifts each, that the cap can be
        # honoured for a whole session — the point is to test the cap, not the
        # fallback that covers a thin pool.
        #
        # The arithmetic matters: a session is six pages of ten, and at two per
        # creator per page one creator can supply twelve. Filling sixty slots
        # without ever yielding therefore needs at least five other creators
        # with twelve Swifts each. Eight with ten apiece clears it with room,
        # and getting this wrong is what made the first version of this test
        # measure the fallback and call it the cap.
        for i in range(8):
            author = make_user(f'other{i}')
            for swift in bulk_swifts(author, 10):
                popular(swift, likes=3, views=50)

        ids, _, _, _ = build_session(self.viewer)
        authors = self._authors_in_order(ids)

        for start in range(0, len(authors), self.window):
            page_authors = authors[start:start + self.window]
            count = page_authors.count(prolific.pk)
            self.assertLessEqual(count, self.rules.max_per_creator_per_page)

    def test_the_same_creator_never_appears_back_to_back(self):
        # Five creators, so a gap of three is satisfiable at all. With two it
        # is arithmetically impossible and the test would be measuring the
        # fallback instead.
        for i in range(5):
            author = make_user(f'creator{i}')
            for swift in bulk_swifts(author, 6):
                popular(swift, likes=10, views=40)

        ids, _, _, _ = build_session(self.viewer)
        authors = self._authors_in_order(ids)

        last_seen = {}
        for position, creator in enumerate(authors):
            if creator in last_seen:
                self.assertGreaterEqual(
                    position - last_seen[creator],
                    self.rules.min_gap_between_same_creator,
                )
            last_seen[creator] = position

    def test_the_caps_yield_rather_than_truncating_the_feed(self):
        """A thin creator pool must not produce a four-item feed.

        Two creators cannot satisfy a three-position gap, and 2-per-page across
        ten slots would cap the whole session at four. Stopping there is the
        worse answer, and it is invisible — the feed simply ends. So the caps
        give way and the viewer gets the Swifts, spaced as well as two creators
        allow.
        """
        for i in range(2):
            author = make_user(f'only{i}')
            for swift in bulk_swifts(author, 10):
                popular(swift, likes=5, views=30)

        ids, _, _, _ = build_session(self.viewer)

        self.assertGreaterEqual(len(ids), 15)
        self.assertEqual(len(ids), len(set(ids)))

    def test_viral_content_does_not_take_every_slot(self):
        viral_author = make_user('viral')
        for swift in bulk_swifts(viral_author, 20):
            popular(swift, likes=10_000, views=20_000)
        for i in range(5):
            other = make_user(f'small{i}')
            for swift in bulk_swifts(other, 3):
                popular(swift, likes=2, views=10)

        ids, _, _, _ = build_session(self.viewer)
        authors = self._authors_in_order(ids)

        self.assertLess(authors.count(viral_author.pk), len(ids))
        self.assertGreater(len(set(authors)), 1)


class ExplorationTests(SwiftTestCase):
    def test_a_new_creator_gets_sourced_even_with_no_engagement(self):
        viewer = make_user('viewer')
        established = make_user('established')
        for swift in bulk_swifts(established, 20):
            popular(swift, likes=1000, views=5000)

        newcomer = make_user('newcomer')
        theirs = make_swift(newcomer)

        _, _, _, source_counts = build_session(viewer)

        self.assertGreaterEqual(source_counts.get('new_creator', 0), 1)


class TrendingTests(SwiftTestCase):
    def test_recent_engagement_beats_old_engagement(self):
        viewer = make_user('viewer')
        author_old = make_user('old')
        author_new = make_user('new')

        stale = popular(make_swift(author_old), likes=400, views=1000)
        age_swift(stale, days=6)
        rising = popular(make_swift(author_new), likes=40, views=100)
        age_swift(rising, hours=2)

        ids, _, _, _ = build_session(viewer)

        self.assertIn(rising.id, ids)
        if stale.id in ids:
            self.assertLess(ids.index(rising.id), ids.index(stale.id))


class ReproducibilityTests(SwiftTestCase):
    def test_the_same_state_produces_the_same_order(self):
        """Ranking is a pure function of the data plus a seed.

        Without this the exploration draw would reshuffle every request and
        pagination could not be stable — the same property, tested at its root.
        """
        viewer = make_user('viewer')
        for i in range(4):
            author = make_user(f'author{i}')
            for swift in bulk_swifts(author, 4):
                popular(swift, likes=i * 3, views=40)

        now = timezone.now()
        first = build_session(viewer, now=now, seed=7)[0]
        SwiftImpression.objects.all().delete()
        second = build_session(viewer, now=now, seed=7)[0]

        self.assertEqual(first, second)


class PaginationTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('viewer')
        author = make_user('author')
        author_b = make_user('author_b')
        for swift in bulk_swifts(author, 10) + bulk_swifts(author_b, 10):
            popular(swift, likes=5, views=30)

    def test_pages_do_not_repeat_across_a_session(self):
        first = page(self.viewer, limit=4)
        self.assertTrue(first.cursor)
        second = page(self.viewer, cursor=first.cursor, limit=4)

        self.assertEqual(set(first.swift_ids) & set(second.swift_ids), set())

    def test_continuing_a_session_does_not_re_rank(self):
        first = page(self.viewer, limit=4)
        second = page(self.viewer, cursor=first.cursor, limit=4)

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)

    def test_an_expired_cursor_starts_a_new_session_rather_than_failing(self):
        result = page(self.viewer, cursor='nonsense.0', limit=4)
        self.assertTrue(result.swift_ids)
        self.assertFalse(result.cache_hit)

    def test_a_malformed_cursor_is_not_an_error(self):
        for bad in ('', '.', 'x.', '.5', 'x.notanumber', 'x.-3'):
            result = page(self.viewer, cursor=bad, limit=2)
            self.assertIsInstance(result.swift_ids, list)

    def test_the_cursor_stops_at_the_end(self):
        seen = []
        cursor = None
        for _ in range(20):
            result = page(self.viewer, cursor=cursor, limit=5)
            seen.extend(result.swift_ids)
            cursor = result.cursor
            if not cursor:
                break
        self.assertIsNone(cursor)
        self.assertEqual(len(seen), len(set(seen)))


@override_settings(SWIFT_RECS={'weights': {'like': 500.0}})
class ParamOverrideTests(SwiftTestCase):
    def test_a_settings_override_actually_moves_the_ranking(self):
        """The tuning knob is connected.

        A weight that can be set but does not change the output is worse than
        no weight at all, because it looks like the parameter was tried.
        """
        viewer = make_user('viewer')
        a, b = make_user('a'), make_user('b')
        liked = popular(make_swift(a), likes=900, views=1000)
        watched = popular(make_swift(b), likes=0, views=1000)
        age_swift(liked, hours=3)
        age_swift(watched, hours=3)

        ids, _, _, _ = build_session(viewer)

        self.assertIn(liked.id, ids)
        self.assertLess(ids.index(liked.id), ids.index(watched.id))
