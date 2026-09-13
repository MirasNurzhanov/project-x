"""L8: the feed gate checks blocks once, and still agrees with the Python gate on every audience case."""
from itertools import product

from .models import SwiftVideo
from .testing import SwiftTestCase, befriend, block, connect, make_swift, make_user
from .visibility import audience_q, can_watch, watchable, watchable_q


class BlockSubqueryShapeTests(SwiftTestCase):
    def test_the_feed_gate_references_blocked_users_once(self):
        viewer = make_user('shape-viewer')
        sql = str(SwiftVideo.objects.filter(watchable_q(viewer)).query)
        self.assertEqual(sql.count('friends_blockeduser'), 1)

    def test_audience_q_alone_keeps_its_own_block_clauses(self):
        viewer = make_user('shape-audience')
        sql = str(SwiftVideo.objects.filter(audience_q(viewer)).query)
        self.assertEqual(sql.count('friends_blockeduser'), 2)


class WatchableMatchesCanWatchTests(SwiftTestCase):
    def test_every_audience_relationship_and_block_combination(self):
        viewer = make_user('matrix-viewer')
        swifts = [make_swift(viewer, visibility=visibility) for visibility in SwiftVideo.Visibility.values]
        combinations = product(
            SwiftVideo.Visibility.values,
            (False, True),                      # author account private
            (False, True),                      # author in Ghost Mode
            ('stranger', 'connected', 'friend'),
            ('none', 'viewer blocked author', 'author blocked viewer'),
        )
        for index, (visibility, private, ghost, relation, blocking) in enumerate(combinations):
            author = make_user(f'matrix-{index}', sharing=not ghost, is_private=private)
            if relation == 'friend':
                befriend(viewer, author)
            elif relation == 'connected':
                connect(viewer, author)
            if blocking == 'viewer blocked author':
                block(viewer, author)
            elif blocking == 'author blocked viewer':
                block(author, viewer)
            swifts.append(make_swift(author, visibility=visibility))

        expected = {swift.pk for swift in swifts if can_watch(viewer, swift)}
        self.assertEqual(set(watchable(viewer).values_list('pk', flat=True)), expected)
        self.assertTrue(expected)
