"""Who may watch what, and who may change it.

Requirements 8 and 12 live here: ownership on the write paths, and the
behaviour of deleted, private and moderated content on every read path.

The rule these all express is `users.visibility`'s: a Swift you cannot see is
indistinguishable from one that does not exist. Every assertion below that
expects a 404 is expecting exactly that, and never a 403.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from .models import SwiftComment, SwiftLike, SwiftVideo
from .testing import (
    SwiftTestCase, befriend, block, connect, make_swift, make_user,
)


class OwnershipTests(SwiftTestCase):
    """Requirement 8 — other users cannot edit or delete a creator's Swift."""

    def setUp(self):
        self.creator = make_user('creator')
        self.stranger = make_user('stranger')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.stranger)
        self.url = reverse('swift-detail', args=[self.swift.pk])

    def test_stranger_cannot_patch(self):
        response = self.client.patch(self.url, {'caption': 'hijacked'}, format='json')

        self.assertEqual(response.status_code, 404)
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.caption, 'a swift')

    def test_stranger_cannot_delete(self):
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 404)
        self.swift.refresh_from_db()
        self.assertIsNone(self.swift.deleted_at)

    def test_a_friend_still_cannot_edit(self):
        """Being allowed to watch is not being allowed to change."""
        befriend(self.creator, self.stranger)
        self.assertEqual(
            self.client.patch(self.url, {'caption': 'x'}, format='json').status_code, 404,
        )

    def test_stranger_cannot_flip_location_sharing_on(self):
        """The most valuable thing to steal on this model."""
        swift = make_swift(self.creator, located=True, shared=False)
        response = self.client.patch(
            reverse('swift-detail', args=[swift.pk]),
            {'location_shared': True}, format='json',
        )

        self.assertEqual(response.status_code, 404)
        swift.refresh_from_db()
        self.assertFalse(swift.location_shared)

    def test_creator_can_patch_and_delete(self):
        client = APIClient()
        client.force_authenticate(self.creator)

        patched = client.patch(self.url, {'caption': 'edited'}, format='json')
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()['caption'], 'edited')

        self.assertEqual(client.delete(self.url).status_code, 204)
        self.swift.refresh_from_db()
        self.assertIsNotNone(self.swift.deleted_at)

    def test_creator_cannot_share_a_location_that_does_not_exist(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        response = client.patch(self.url, {'location_shared': True}, format='json')

        self.assertEqual(response.status_code, 400)


class DeletedAndModeratedTests(SwiftTestCase):
    """Requirement 12, first half."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def _feed_ids(self, client=None):
        body = (client or self.client).get(
            reverse('swift-feed'), {'chrono': '1'},
        ).json()
        return [row['id'] for row in body['results']]

    def test_soft_deleted_is_gone_from_the_feed_and_the_detail(self):
        self.assertIn(self.swift.pk, self._feed_ids())

        self.swift.deleted_at = self.swift.created_at
        self.swift.save(update_fields=['deleted_at'])

        self.assertNotIn(self.swift.pk, self._feed_ids())
        self.assertEqual(
            self.client.get(reverse('swift-detail', args=[self.swift.pk])).status_code,
            404,
        )

    def test_removed_is_gone_for_everyone_including_the_creator(self):
        """A takedown the author can still browse is not a takedown."""
        self.swift.moderation = SwiftVideo.Moderation.REMOVED
        self.swift.save(update_fields=['moderation'])

        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)

        self.assertNotIn(self.swift.pk, self._feed_ids())
        self.assertNotIn(self.swift.pk, self._feed_ids(creator_client))
        self.assertEqual(
            creator_client.get(
                reverse('swift-detail', args=[self.swift.pk])).status_code, 404,
        )
        mine = creator_client.get(reverse('swift-mine')).json()
        self.assertEqual(mine['count'], 0)

    def test_only_explicitly_approved_swifts_are_public(self):
        from . import visibility

        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)
        self.assertIn(self.swift.pk, self._feed_ids())
        states = [state for state in SwiftVideo.Moderation
                  if state != SwiftVideo.Moderation.OK] + ['', 'failed', 'unknown']
        for state in states:
            with self.subTest(moderation=state):
                self.swift.moderation = state
                self.swift.save(update_fields=['moderation'])
                self.assertFalse(self.swift.is_live)
                self.assertFalse(visibility.can_watch(self.viewer, self.swift))
                self.assertFalse(visibility.can_watch(self.creator, self.swift))
                self.assertNotIn(self.swift.pk, self._feed_ids())
                self.assertNotIn(self.swift.pk, self._feed_ids(creator_client))
                self.assertEqual(self.client.get(
                    reverse('swift-detail', args=[self.swift.pk])).status_code, 404)
                for endpoint in ('swift-like', 'swift-repost', 'swift-view'):
                    self.assertEqual(self.client.post(
                        reverse(endpoint, args=[self.swift.pk])).status_code, 404)

    def test_ranked_feed_rechecks_approval_for_cached_ids(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        approved = make_swift(self.creator)
        page = SimpleNamespace(
            swift_ids=[self.swift.pk, approved.pk], cursor=None,
            has_more=False, sources_by_id={},
        )
        for state in ('pending', 'quarantined', 'flagged', 'failed', 'unknown'):
            with self.subTest(moderation=state):
                self.swift.moderation = state
                self.swift.save(update_fields=['moderation'])
                with patch('recs.pipeline.page', return_value=page):
                    response = self.client.get(reverse('swift-feed'))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [row['id'] for row in response.json()['results']], [approved.pk])

    def test_creator_can_check_review_status_without_publishing(self):
        self.client.force_authenticate(self.creator)
        for state, review in (
            ('pending', 'reviewing'), ('quarantined', 'needs_review'),
            ('flagged', 'needs_review'), ('unknown', 'needs_review'),
        ):
            with self.subTest(moderation=state):
                self.swift.moderation = state
                self.swift.save(update_fields=['moderation'])
                detail = self.client.get(reverse('swift-detail', args=[self.swift.pk]))
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()['review_state'], review)
                mine = self.client.get(reverse('swift-mine')).json()['results']
                self.assertEqual(mine[0]['review_state'], review)
                self.assertNotIn(self.swift.pk, self._feed_ids())

    def test_engagement_on_a_deleted_swift_is_refused(self):
        self.swift.deleted_at = self.swift.created_at
        self.swift.save(update_fields=['deleted_at'])

        for name in ('swift-like', 'swift-repost', 'swift-view'):
            with self.subTest(endpoint=name):
                response = self.client.post(reverse(name, args=[self.swift.pk]))
                self.assertEqual(response.status_code, 404)

        self.assertEqual(SwiftLike.objects.count(), 0)
        comment = self.client.post(
            reverse('swift-comments', args=[self.swift.pk]),
            {'text': 'x'}, format='json',
        )
        self.assertEqual(comment.status_code, 404)
        self.assertEqual(SwiftComment.objects.count(), 0)

    def test_still_processing_is_not_in_anyone_elses_feed(self):
        processing = make_swift(self.creator, status=SwiftVideo.Status.PROCESSING)
        self.assertNotIn(processing.pk, self._feed_ids())

    def test_creator_sees_their_own_processing_swift_in_mine(self):
        """The one surface that shows a creator a row the feed withholds."""
        processing = make_swift(self.creator, status=SwiftVideo.Status.PROCESSING)
        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)

        ids = [r['id'] for r in creator_client.get(reverse('swift-mine')).json()['results']]
        self.assertIn(processing.pk, ids)


class AudienceTests(SwiftTestCase):
    """Requirement 12, second half — private and friends-only content."""

    def setUp(self):
        self.creator = make_user('creator')
        self.stranger = make_user('stranger')
        self.friend = make_user('friend')
        befriend(self.creator, self.friend)

        self.stranger_client = APIClient()
        self.stranger_client.force_authenticate(self.stranger)
        self.friend_client = APIClient()
        self.friend_client.force_authenticate(self.friend)

    def _ids(self, client):
        # The unranked path: this helper answers "may they see it", and
        # permission is identical on both. Ranking is not — it personalises,
        # never repeats a Swift, and leaves out the viewer's own content.
        return [
            r['id'] for r in
            client.get(reverse('swift-feed'), {'chrono': '1'}).json()['results']
        ]

    def test_private_swift_reaches_nobody_but_the_creator(self):
        swift = make_swift(self.creator, visibility=SwiftVideo.Visibility.PRIVATE)

        self.assertNotIn(swift.pk, self._ids(self.stranger_client))
        self.assertNotIn(swift.pk, self._ids(self.friend_client))
        self.assertEqual(
            self.stranger_client.get(
                reverse('swift-detail', args=[swift.pk])).status_code, 404,
        )

        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)
        self.assertIn(swift.pk, self._ids(creator_client))

    def test_friends_only_swift_reaches_friends_and_not_strangers(self):
        swift = make_swift(self.creator, visibility=SwiftVideo.Visibility.FRIENDS)

        self.assertIn(swift.pk, self._ids(self.friend_client))
        self.assertNotIn(swift.pk, self._ids(self.stranger_client))

    def test_a_hi_connection_is_not_a_friend(self):
        """Same line `users.visibility` draws for stories: a Hi is a hello."""
        connected = make_user('connected')
        connect(self.creator, connected)
        swift = make_swift(self.creator, visibility=SwiftVideo.Visibility.FRIENDS)

        client = APIClient()
        client.force_authenticate(connected)
        self.assertNotIn(swift.pk, self._ids(client))

    def test_private_account_public_swift_reaches_approved_viewers_only(self):
        """Posting does not undo the account switch."""
        self.creator.is_private = True
        self.creator.save(update_fields=['is_private'])
        swift = make_swift(self.creator)

        self.assertNotIn(swift.pk, self._ids(self.stranger_client))
        self.assertIn(swift.pk, self._ids(self.friend_client))

    def test_blocking_hides_in_both_directions(self):
        swift = make_swift(self.creator)
        blocked_back = make_swift(self.stranger)
        block(self.creator, self.stranger)

        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)

        self.assertNotIn(swift.pk, self._ids(self.stranger_client))
        self.assertNotIn(blocked_back.pk, self._ids(creator_client))

    def test_engagement_on_an_unwatchable_swift_is_refused(self):
        swift = make_swift(self.creator, visibility=SwiftVideo.Visibility.PRIVATE)

        for name in ('swift-like', 'swift-repost', 'swift-view'):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.stranger_client.post(reverse(name, args=[swift.pk])).status_code,
                    404,
                )


class CommentVisibilityTests(SwiftTestCase):
    def test_blocked_authors_comments_are_hidden(self):
        creator = make_user('creator')
        viewer = make_user('viewer')
        rude = make_user('rude')
        swift = make_swift(creator)

        rude_client = APIClient()
        rude_client.force_authenticate(rude)
        rude_client.post(
            reverse('swift-comments', args=[swift.pk]), {'text': 'grr'}, format='json')

        block(viewer, rude)

        client = APIClient()
        client.force_authenticate(viewer)
        body = client.get(reverse('swift-comments', args=[swift.pk])).json()
        self.assertEqual(body['results'], [])


class FeedShapeTests(SwiftTestCase):
    """The list contract the client is going to be written against."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def test_feed_is_newest_first(self):
        first = make_swift(self.creator, caption='older')
        second = make_swift(self.creator, caption='newer')

        ids = [
            r['id'] for r in
            self.client.get(reverse('swift-feed'), {'chrono': '1'}).json()['results']
        ]
        self.assertEqual(ids, [second.pk, first.pk])

    def test_paging_is_bounded(self):
        for _ in range(3):
            make_swift(self.creator)

        body = self.client.get(f"{reverse('swift-feed')}?chrono=1&limit=2").json()
        self.assertEqual(body['count'], 3)
        self.assertEqual(len(body['results']), 2)

    def test_limit_is_clamped_to_the_ceiling(self):
        body = self.client.get(f"{reverse('swift-feed')}?chrono=1&limit=9999").json()
        self.assertEqual(body['results'], [])

    def test_garbage_paging_parameters_do_not_500(self):
        response = self.client.get(f"{reverse('swift-feed')}?chrono=1&limit=abc&offset=-4")
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_access_is_refused(self):
        """Approx has no anonymous content surface; Swifts does not add one."""
        anon = APIClient()
        for name, args in (
            ('swift-feed', []), ('swift-mine', []),
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(anon.get(reverse(name, args=args)).status_code, 401)

        swift = make_swift(self.creator)
        self.assertEqual(
            anon.post(reverse('swift-reveal-location', args=[swift.pk])).status_code, 401,
        )
