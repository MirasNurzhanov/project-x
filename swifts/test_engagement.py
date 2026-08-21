"""Likes, reposts, comments and views — the duplicate and counter rules.

The unique constraints are the correctness boundary here, and these tests
exercise them through the API rather than by creating rows directly, because
the thing being asserted is that *a client hammering an endpoint* cannot
produce a second row or a wrong count.
"""
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APIClient

from .counters import adjust
from .models import (
    SwiftComment, SwiftLike, SwiftRepost, SwiftVideo, SwiftView,
)
from .testing import SwiftTestCase, make_swift, make_user


class CounterHelperTests(SwiftTestCase):
    """`counters.adjust` on its own — where the concurrency rule lives."""

    def setUp(self):
        self.swift = make_swift(make_user('creator'))

    def count(self, field='like_count'):
        return SwiftVideo.objects.values_list(field, flat=True).get(pk=self.swift.pk)

    def test_increments(self):
        adjust(self.swift.pk, 'like_count', 1)
        self.assertEqual(self.count(), 1)

    def test_decrements(self):
        SwiftVideo.objects.filter(pk=self.swift.pk).update(like_count=3)
        adjust(self.swift.pk, 'like_count', -1)
        self.assertEqual(self.count(), 2)

    def test_never_goes_negative(self):
        adjust(self.swift.pk, 'like_count', -1)
        self.assertEqual(self.count(), 0)

    def test_does_not_lose_a_concurrent_write(self):
        """The regression this helper exists for.

        `self.swift` deliberately holds a stale count while the database has
        moved on — which is exactly the state two overlapping requests are in.
        The read-modify-write form would compute 0 + 1 from the stale value and
        write 1, discarding the four in between.
        """
        SwiftVideo.objects.filter(pk=self.swift.pk).update(like_count=4)
        adjust(self.swift.pk, 'like_count', 1)
        self.assertEqual(self.count(), 5)

    def test_rejects_a_field_that_is_not_a_counter(self):
        with self.assertRaises(ValueError):
            adjust(self.swift.pk, 'caption', 1)


class LikeTests(SwiftTestCase):
    """Requirement 9 — duplicate likes do not duplicate rows."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-like', args=[self.swift.pk])

    def reread(self):
        return SwiftVideo.objects.get(pk=self.swift.pk)

    def test_like_creates_one_row_and_moves_the_counter(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['like_count'], 1)
        self.assertEqual(SwiftLike.objects.count(), 1)
        self.assertEqual(self.reread().like_count, 1)

    def test_liking_five_times_leaves_one_row_and_a_count_of_one(self):
        """The double-tap case, and the retry case, and the flaky-network case."""
        for _ in range(5):
            self.client.post(self.url)

        self.assertEqual(SwiftLike.objects.count(), 1)
        self.assertEqual(self.reread().like_count, 1)

    def test_repeat_like_answers_200_not_201(self):
        self.assertEqual(self.client.post(self.url).status_code, 201)
        self.assertEqual(self.client.post(self.url).status_code, 200)

    def test_database_refuses_a_second_like_row(self):
        """The constraint, directly. The view's `get_or_create` is a
        convenience on top of this — the guarantee is here."""
        SwiftLike.objects.create(swift=self.swift, user=self.viewer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SwiftLike.objects.create(swift=self.swift, user=self.viewer)

    def test_unlike_removes_the_row_and_the_count(self):
        self.client.post(self.url)
        response = self.client.delete(self.url)

        self.assertEqual(response.json()['like_count'], 0)
        self.assertEqual(SwiftLike.objects.count(), 0)

    def test_unliking_twice_does_not_drive_the_count_negative(self):
        self.client.post(self.url)
        self.client.delete(self.url)
        self.client.delete(self.url)

        self.assertEqual(self.reread().like_count, 0)

    def test_two_people_liking_are_both_counted(self):
        other = make_user('other')
        second = APIClient()
        second.force_authenticate(other)

        self.client.post(self.url)
        second.post(self.url)

        self.assertEqual(SwiftLike.objects.count(), 2)
        self.assertEqual(self.reread().like_count, 2)

    def test_viewer_liked_flag_reflects_the_like(self):
        feed = self.client.get(reverse('swift-feed')).json()
        self.assertFalse(feed['results'][0]['viewer_liked'])

        self.client.post(self.url)
        feed = self.client.get(reverse('swift-feed')).json()
        self.assertTrue(feed['results'][0]['viewer_liked'])


class RepostTests(SwiftTestCase):
    """Requirement 10 — duplicate reposts do not duplicate rows."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-repost', args=[self.swift.pk])

    def test_reposting_five_times_leaves_one_row(self):
        for _ in range(5):
            self.client.post(self.url)

        self.assertEqual(SwiftRepost.objects.count(), 1)
        self.assertEqual(
            SwiftVideo.objects.get(pk=self.swift.pk).repost_count, 1,
        )

    def test_database_refuses_a_second_repost_row(self):
        SwiftRepost.objects.create(swift=self.swift, user=self.viewer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SwiftRepost.objects.create(swift=self.swift, user=self.viewer)

    def test_unrepost_removes_the_row(self):
        self.client.post(self.url)
        response = self.client.delete(self.url)

        self.assertEqual(response.json()['repost_count'], 0)
        self.assertEqual(SwiftRepost.objects.count(), 0)

    def test_creator_cannot_repost_their_own(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        response = client.post(self.url)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SwiftRepost.objects.count(), 0)

    def test_reposting_is_refused_when_the_creator_turned_it_off(self):
        self.swift.allow_reposts = False
        self.swift.save(update_fields=['allow_reposts'])

        self.assertEqual(self.client.post(self.url).status_code, 403)


class ViewTests(SwiftTestCase):
    """Views must not accumulate a row per render.

    This is the requirement stated as "views should not create unlimited
    duplicate rows every time React rerenders", and it is met by the unique
    constraint rather than by any client-side debounce.
    """

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-view', args=[self.swift.pk])

    def reread(self):
        return SwiftVideo.objects.get(pk=self.swift.pk)

    def test_first_view_creates_a_row_and_moves_the_counter(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['first_view'])
        self.assertEqual(SwiftView.objects.count(), 1)
        self.assertEqual(self.reread().view_count, 1)

    def test_twenty_reports_leave_one_row_and_a_view_count_of_one(self):
        """A remount storm. One row, one distinct viewer, count of one."""
        for _ in range(20):
            self.client.post(self.url)

        self.assertEqual(SwiftView.objects.count(), 1)
        self.assertEqual(self.reread().view_count, 1)

    def test_repeat_reports_are_counted_on_the_view_row(self):
        """Not discarded — a rewatch is real signal, it just is not a new row."""
        for _ in range(4):
            self.client.post(self.url)

        row = SwiftView.objects.get(swift=self.swift, viewer=self.viewer)
        self.assertEqual(row.view_count, 4)

    def test_repeat_report_answers_200_not_201(self):
        self.assertEqual(self.client.post(self.url).status_code, 201)
        self.assertEqual(self.client.post(self.url).status_code, 200)

    def test_watch_ms_is_a_high_water_mark(self):
        """Replaying a Swift cannot inflate the reported watch time."""
        self.client.post(self.url, {'watch_ms': 8000}, format='json')
        self.client.post(self.url, {'watch_ms': 3000}, format='json')

        row = SwiftView.objects.get(swift=self.swift, viewer=self.viewer)
        self.assertEqual(row.watch_ms, 8000)

        self.client.post(self.url, {'watch_ms': 12000}, format='json')
        row.refresh_from_db()
        self.assertEqual(row.watch_ms, 12000)

    def test_completed_latches_on(self):
        self.client.post(self.url, {'completed': True}, format='json')
        self.client.post(self.url, {'completed': False}, format='json')

        row = SwiftView.objects.get(swift=self.swift, viewer=self.viewer)
        self.assertTrue(row.completed)

    def test_two_viewers_are_two_rows_and_a_count_of_two(self):
        other = make_user('other')
        second = APIClient()
        second.force_authenticate(other)

        self.client.post(self.url)
        second.post(self.url)

        self.assertEqual(SwiftView.objects.count(), 2)
        self.assertEqual(self.reread().view_count, 2)

    def test_database_refuses_a_second_view_row(self):
        SwiftView.objects.create(swift=self.swift, viewer=self.viewer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SwiftView.objects.create(swift=self.swift, viewer=self.viewer)

    def test_cannot_record_a_view_of_a_swift_you_cannot_watch(self):
        """The gate on the writer, which is what keeps the reveal gate honest.

        A view row written for an unwatchable Swift would be a key cut for a
        door the viewer should never reach.
        """
        private = make_swift(self.creator, visibility=SwiftVideo.Visibility.PRIVATE)
        response = self.client.post(reverse('swift-view', args=[private.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(SwiftView.objects.filter(swift=private).exists())


class CommentTests(SwiftTestCase):
    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-comments', args=[self.swift.pk])

    def reread(self):
        return SwiftVideo.objects.get(pk=self.swift.pk)

    def test_posting_a_comment_moves_the_counter(self):
        response = self.client.post(self.url, {'text': 'nice'}, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.reread().comment_count, 1)

    def test_empty_comment_is_rejected(self):
        response = self.client.post(self.url, {'text': '   '}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_author_can_delete_own_comment(self):
        comment_id = self.client.post(
            self.url, {'text': 'mine'}, format='json').json()['id']

        response = self.client.delete(
            reverse('swift-comment-delete', args=[comment_id]))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.reread().comment_count, 0)
        # Soft-deleted, not gone — replies keep their place in the thread.
        self.assertTrue(SwiftComment.objects.filter(pk=comment_id).exists())
        self.assertNotIn(
            comment_id, [c['id'] for c in self.client.get(self.url).json()['results']],
        )

    def test_swift_creator_can_delete_someone_elses_comment(self):
        comment_id = self.client.post(
            self.url, {'text': 'rude'}, format='json').json()['id']

        creator_client = APIClient()
        creator_client.force_authenticate(self.creator)
        response = creator_client.delete(
            reverse('swift-comment-delete', args=[comment_id]))

        self.assertEqual(response.status_code, 204)

    def test_stranger_cannot_delete_a_comment(self):
        comment_id = self.client.post(
            self.url, {'text': 'mine'}, format='json').json()['id']

        stranger = APIClient()
        stranger.force_authenticate(make_user('stranger'))
        response = stranger.delete(reverse('swift-comment-delete', args=[comment_id]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.reread().comment_count, 1)

    def test_comments_are_refused_when_the_creator_turned_them_off(self):
        self.swift.allow_comments = False
        self.swift.save(update_fields=['allow_comments'])

        self.assertEqual(
            self.client.post(self.url, {'text': 'hi'}, format='json').status_code, 403,
        )

    def test_replies_are_flattened_to_one_level(self):
        top = self.client.post(self.url, {'text': 'top'}, format='json').json()
        reply = self.client.post(
            self.url, {'text': 'reply', 'parent': top['id']}, format='json').json()
        deep = self.client.post(
            self.url, {'text': 'deep', 'parent': reply['id']}, format='json').json()

        self.assertEqual(reply['parent'], top['id'])
        self.assertEqual(deep['parent'], top['id'])

    def test_reply_to_a_comment_on_another_swift_is_rejected(self):
        other = make_swift(self.creator)
        elsewhere = self.client.post(
            reverse('swift-comments', args=[other.pk]), {'text': 'x'}, format='json',
        ).json()

        response = self.client.post(
            self.url, {'text': 'y', 'parent': elsewhere['id']}, format='json')
        self.assertEqual(response.status_code, 400)
