"""Lazy feed queries must check permissions when their rows are fetched."""
from friends.models import BlockedUser, FriendRequest, HiRequest

from .models import SwiftVideo
from .testing import SwiftTestCase, befriend, connect, make_swift, make_user
from .visibility import watchable


class LazyWatchabilityTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.viewer = make_user('query-viewer')
        self.author = make_user('query-author')

    def test_blocks_added_before_evaluation_apply_in_both_directions(self):
        swift = make_swift(self.author)
        for blocker, blocked in (
            (self.viewer, self.author), (self.author, self.viewer),
        ):
            with self.subTest(blocker=blocker.pk):
                pending_query = watchable(self.viewer).filter(pk=swift.pk)
                pair = BlockedUser.objects.create(blocker=blocker, blocked=blocked)
                self.assertEqual(list(pending_query), [])
                pair.delete()
                self.assertEqual(list(watchable(self.viewer)), [swift])

    def test_revoked_friendship_does_not_survive_in_a_pending_query(self):
        swift = make_swift(self.author, visibility=SwiftVideo.Visibility.FRIENDS)
        befriend(self.viewer, self.author)
        pending_query = watchable(self.viewer).filter(pk=swift.pk)
        FriendRequest.objects.all().delete()
        self.assertEqual(list(pending_query), [])

    def test_revoked_hi_no_longer_opens_a_private_account(self):
        self.author.is_private = True
        self.author.save(update_fields=['is_private'])
        swift = make_swift(self.author)
        connect(self.author, self.viewer)
        self.assertEqual(list(watchable(self.viewer)), [swift])
        pending_query = watchable(self.viewer).filter(pk=swift.pk)
        HiRequest.objects.all().delete()
        self.assertEqual(list(pending_query), [])
