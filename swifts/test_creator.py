from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from .models import SwiftComment, SwiftLike, SwiftRepost, SwiftVideo, SwiftView
from .testing import SwiftTestCase, befriend, block, connect, find_forbidden, make_swift, make_user


class CreatorGalleryTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.owner = make_user('creator')
        self.viewer = make_user('profile-viewer')
        self.url = f'/api/swifts/creator/{self.owner.pk}/'

    def test_owner_sees_their_uploads_and_statuses_but_not_removals(self):
        self.client.force_authenticate(self.owner)
        visible = [
            make_swift(self.owner),
            make_swift(self.owner, visibility='private', status='processing', moderation='pending'),
            make_swift(self.owner, moderation='quarantined'),
            make_swift(self.owner, status='failed'),
        ]
        make_swift(self.owner, moderation='removed')
        make_swift(self.owner, deleted_at=datetime.now(timezone.utc))
        make_swift(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual([s['id'] for s in response.data['results']], [s.pk for s in visible])
        self.assertEqual(response.data['count'], 4)
        self.assertTrue(all(s['is_mine'] for s in response.data['results']))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(find_forbidden(response.data), [])

    def test_watching_and_saving_somebody_else_s_swift_keeps_it_off_your_own_gallery(self):
        """A profile gallery is one person's uploads — not what they have watched.

        Watching writes a `SwiftView` and saving writes a `SavedSwift`; neither
        is authorship, and neither may put another creator's video on the
        viewer's own profile.
        """
        self.client.force_authenticate(self.viewer)
        theirs = make_swift(self.owner)
        mine = make_swift(self.viewer)

        self.assertEqual(self.client.post(f'/api/swifts/{theirs.pk}/view/').status_code, 201)
        self.assertEqual(self.client.post(f'/api/swifts/{theirs.pk}/save/').status_code, 201)

        response = self.client.get(f'/api/swifts/creator/{self.viewer.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['id'] for s in response.data['results']], [mine.pk])
        self.assertEqual(response.data['count'], 1)
        # The saved one is still on the private shortlist, which is a different list.
        saved = self.client.get('/api/swifts/saved/')
        self.assertEqual([s['id'] for s in saved.data['results']], [theirs.pk])

    def test_a_gallery_only_ever_carries_one_author(self):
        self.client.force_authenticate(self.viewer)
        theirs = make_swift(self.owner)
        make_swift(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual([s['id'] for s in response.data['results']], [theirs.pk])
        self.assertEqual(
            {s['author']['id'] for s in response.data['results']}, {self.owner.pk},
        )

    def test_other_viewers_only_receive_publishable_audience_allowed_swifts(self):
        self.client.force_authenticate(self.viewer)
        public = make_swift(self.owner)
        friends = make_swift(self.owner, visibility='friends')
        make_swift(self.owner, visibility='private')
        make_swift(self.owner, moderation='pending')
        make_swift(self.owner, moderation='flagged')
        make_swift(self.owner, moderation='quarantined')
        make_swift(self.owner, status='processing')
        response = self.client.get(self.url)
        self.assertEqual([s['id'] for s in response.data['results']], [public.pk])
        befriend(self.viewer, self.owner)
        response = self.client.get(self.url)
        self.assertCountEqual([s['id'] for s in response.data['results']], [public.pk, friends.pk])
        self.assertEqual(find_forbidden(response.data), [])

    def test_private_profiles_require_approval_and_ghost_mode_hides_even_from_friends(self):
        make_swift(self.owner)
        self.client.force_authenticate(self.viewer)
        self.owner.is_private = True
        self.owner.save(update_fields=['is_private'])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        connect(self.viewer, self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        befriend(self.viewer, self.owner)
        self.owner.ghost_mode = True
        self.owner.save(update_fields=['ghost_mode'])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.url).data['count'], 1)

    def test_blocks_and_authentication_are_enforced(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.viewer)
        block(self.owner, self.viewer)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_pagination_is_stable_for_equal_upload_timestamps(self):
        stamp = datetime(2026, 9, 9, tzinfo=timezone.utc)
        videos = [make_swift(self.owner) for _ in range(4)]
        SwiftVideo.objects.filter(author=self.owner).update(created_at=stamp)
        self.client.force_authenticate(self.owner)
        pages = [self.client.get(self.url, {'limit': 2, 'offset': offset}).data for offset in (0, 2)]
        self.assertEqual([s['id'] for page in pages for s in page['results']],
                         [s.pk for s in reversed(videos)])
        self.assertEqual([page['count'] for page in pages], [4, 4])


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


class CreatorAnalyticsTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.owner = make_user('analytics-owner')
        self.viewer = make_user('analytics-viewer')
        self.client.force_authenticate(self.owner)
        self.url = '/api/swifts/analytics/'
        clock = patch('swifts.creator.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def stamp(obj, field, value):
        type(obj).objects.filter(pk=obj.pk).update(**{field: value})
        return obj

    def test_zero_filled_empty_ranges(self):
        for days in (7, 28, 90):
            response = self.client.get(self.url, {'days': days})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.data['series']), days)
            self.assertTrue(all(row['views'] == row['likes'] == row['comments'] == row['reposts'] == 0
                                for row in response.data['series']))
            self.assertEqual(response.data['totals']['videos'], 0)
            self.assertEqual(response.data['top_swifts'], [])

    def test_counts_distinct_first_views_and_retained_actions_on_utc_dates(self):
        video = make_swift(self.owner, view_count=3, like_count=1, comment_count=1, repost_count=1)
        before_start = datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
        start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        people = [self.viewer, make_user('previous-viewer'), make_user('boundary-viewer')]
        self.stamp(SwiftView.objects.create(swift=video, viewer=people[0], view_count=20),
                   'first_viewed_at', NOW)
        self.stamp(SwiftView.objects.create(swift=video, viewer=people[1]), 'first_viewed_at', before_start)
        self.stamp(SwiftView.objects.create(swift=video, viewer=people[2]), 'first_viewed_at', start)
        self.stamp(SwiftLike.objects.create(swift=video, user=self.viewer), 'created_at', NOW)
        self.stamp(SwiftComment.objects.create(swift=video, author=self.viewer, text='hello'),
                   'created_at', NOW)
        self.stamp(SwiftRepost.objects.create(swift=video, user=self.viewer), 'created_at', before_start)
        SwiftComment.objects.create(swift=video, author=self.viewer, text='hidden', moderation='quarantined')
        SwiftComment.objects.create(swift=video, author=self.viewer, text='gone', deleted_at=NOW)
        response = self.client.get(self.url)
        self.assertEqual(response.data['period'], {'views': 2, 'likes': 1, 'comments': 1, 'reposts': 0})
        self.assertEqual(response.data['previous_period'], {'views': 1, 'likes': 0, 'comments': 0, 'reposts': 1})
        self.assertEqual(response.data['series'][0]['date'], '2026-09-03')
        self.assertEqual(response.data['series'][0]['views'], 1)
        self.assertEqual(response.data['series'][-1]['views'], 1)
        self.assertEqual(response.data['totals']['views'], 3)
        self.assertEqual(response.data['top_swifts'][0]['views'], 2)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(find_forbidden(response.data), [])
        self.assertNotIn('viewer_id', str(response.data))

    def test_deleted_removed_and_other_creators_are_excluded(self):
        kept = make_swift(self.owner, view_count=2)
        excluded = [
            make_swift(self.owner, moderation='removed', view_count=100),
            make_swift(self.owner, deleted_at=NOW, view_count=100),
            make_swift(self.viewer, view_count=100),
        ]
        for video in [kept, *excluded]:
            SwiftView.objects.create(swift=video, viewer=self.viewer)
        response = self.client.get(self.url, {'creator_id': self.viewer.pk})
        self.assertEqual(response.data['totals']['videos'], 1)
        self.assertEqual(response.data['totals']['views'], 2)
        self.assertEqual(response.data['period']['views'], 1)
        self.assertEqual([s['id'] for s in response.data['top_swifts']], [kept.pk])

    def test_analytics_are_owner_only_but_accessible_in_ghost_mode(self):
        video = make_swift(self.owner, visibility='private')
        self.owner.ghost_mode = True
        self.owner.save(update_fields=['ghost_mode'])
        self.assertEqual(self.client.get(self.url, {'swift_id': video.pk}).status_code, 200)
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(self.url, {'swift_id': video.pk}).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_invalid_ranges_and_ids_are_rejected(self):
        for params in ({'days': 'invalid'}, {'days': 100000}, {'days': 0},
                       {'swift_id': 'bad'}, {'swift_id': -1}):
            self.assertEqual(self.client.get(self.url, params).status_code, 400)

    def test_period_queries_ignore_future_dates_and_do_not_grow_per_video(self):
        for index in range(15):
            video = make_swift(self.owner)
            self.stamp(SwiftView.objects.create(swift=video, viewer=self.viewer),
                       'first_viewed_at', NOW + timedelta(days=1))
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['period']['views'], 0)
        self.assertLessEqual(len(queries), 8)
