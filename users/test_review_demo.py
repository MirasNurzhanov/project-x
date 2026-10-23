"""The App Review demo (guideline 2.1(a)): a world of its own that follows the reviewer."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from clubs.models import Club, ClubMember
from friends.models import FriendRequest, HiRequest
from interests.models import Interest
from notifications.models import Notification
from stories.models import Story
from swifts import visibility as swift_visibility
from swifts.models import SwiftVideo
from users import review_demo, visibility
from users.models import User

LOCATION_URL = '/api/proximity/location/'
QUERY_URL = '/api/proximity/nearby/query/'
REVIEWER_EMAIL = 'review@example.com'
# Somewhere no seeded offset is measured from, so a move is a real move.
CUPERTINO = ('37.33490000', '-122.00900000')


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def seed(**extra):
    call_command(
        'seed_review_demo', email=REVIEWER_EMAIL, password='review-pass-123',
        stdout=StringIO(), **extra,
    )
    return User.objects.get(email=REVIEWER_EMAIL)


def report(user, lat, lon):
    response = client_for(user).patch(
        LOCATION_URL, {'latitude': lat, 'longitude': lon, 'accuracy': 10}, format='json',
    )
    assert response.status_code == 200, response.content
    user.refresh_from_db()


def seen_by(viewer, lat, lon):
    response = client_for(viewer).post(QUERY_URL, {'latitude': lat, 'longitude': lon}, format='json')
    assert response.status_code == 200, response.content
    return {row['username'] for row in response.json()}


class SeedTests(TestCase):
    def setUp(self):
        for name in ('Hiking', 'Running', 'Board Games', 'Photography'):
            Interest.objects.get_or_create(name=name)

    def test_creates_the_reviewer_and_the_cast(self):
        reviewer = seed()
        self.assertTrue(reviewer.is_review_demo)
        self.assertTrue(reviewer.check_password('review-pass-123'))
        self.assertEqual(reviewer.age_tier, User.AgeTier.ADULT)
        cast = User.objects.filter(username__in=[m.username for m in review_demo.CAST])
        self.assertEqual(cast.count(), len(review_demo.CAST))
        self.assertTrue(all(u.is_review_demo and not u.has_usable_password() for u in cast))
        self.assertEqual(
            FriendRequest.objects.filter(receiver=reviewer, status=FriendRequest.Status.ACCEPTED).count(), 3,
        )
        self.assertTrue(FriendRequest.objects.filter(
            receiver=reviewer, status=FriendRequest.Status.PENDING).exists())
        self.assertTrue(HiRequest.objects.filter(receiver=reviewer, status=HiRequest.Status.ACCEPTED).exists())
        roles = dict(ClubMember.objects.filter(user=reviewer).values_list('club__name', 'role'))
        self.assertEqual(sorted(roles.values()), [ClubMember.Role.MEMBER, ClubMember.Role.OWNER])

    def test_running_it_again_changes_nothing_but_the_password(self):
        seed()
        counts = (User.objects.count(), FriendRequest.objects.count(), Club.objects.count(), Story.objects.count())
        call_command('seed_review_demo', email=REVIEWER_EMAIL, password='another-pass-456', stdout=StringIO())
        self.assertEqual(
            counts,
            (User.objects.count(), FriendRequest.objects.count(), Club.objects.count(), Story.objects.count()),
        )
        self.assertTrue(User.objects.get(email=REVIEWER_EMAIL).check_password('another-pass-456'))

    def test_refuses_to_take_over_a_real_account(self):
        User.objects.create_user(username='someone', email=REVIEWER_EMAIL, password='x-123456')
        with self.assertRaises(CommandError):
            seed()
        self.assertFalse(User.objects.filter(is_review_demo=True).exists())

    def test_demo_clubs_cannot_be_discovered(self):
        seed()
        for club in Club.objects.all():
            self.assertNotEqual(club.visibility_level, Club.Visibility.GLOBAL)
            self.assertIsNone(club.visibility_city_id)
            self.assertEqual(club.visibility_city_osm, '')

    def test_remove_deletes_the_demo_and_nothing_else(self):
        real = User.objects.create_user(username='real', email='real@example.com', password='x-123456')
        real_club = Club.objects.create(name='Real club', creator=real)
        seed()
        call_command('seed_review_demo', remove=True, stdout=StringIO())
        self.assertEqual(list(User.objects.all()), [real])
        self.assertEqual(list(Club.objects.all()), [real_club])


class IsolationTests(TestCase):
    def setUp(self):
        self.real = User.objects.create_user(username='real', email='real@example.com', password='x-123456')
        self.demo = User.objects.create_user(username='demo', email='demo@example.com', password='x-123456')
        User.objects.filter(pk=self.demo.pk).update(is_review_demo=True)
        self.demo.refresh_from_db()

    def test_neither_world_receives_the_other(self):
        self.assertNotIn(self.demo, visibility.visible_users(self.real))
        self.assertNotIn(self.real, visibility.visible_users(self.demo))

    def test_neither_world_can_watch_the_others_swifts(self):
        def swift(author):
            return SwiftVideo.objects.create(
                author=author, status=SwiftVideo.Status.READY, moderation=SwiftVideo.Moderation.OK,
                visibility=SwiftVideo.Visibility.PUBLIC,
            )

        demo_swift, real_swift = swift(self.demo), swift(self.real)
        self.assertEqual(list(swift_visibility.watchable(self.real)), [real_swift])
        self.assertEqual(list(swift_visibility.watchable(self.demo)), [demo_swift])
        self.assertFalse(swift_visibility.can_watch(self.real, demo_swift))
        self.assertFalse(swift_visibility.can_watch(self.demo, real_swift))
        self.assertTrue(swift_visibility.can_watch(self.demo, demo_swift))


class FollowTheReviewerTests(TestCase):
    def setUp(self):
        Interest.objects.get_or_create(name='Hiking')
        self.reviewer = seed()

    def test_friends_appear_next_to_the_reviewer_wherever_they_are(self):
        report(self.reviewer, *CUPERTINO)
        seen = seen_by(self.reviewer, *CUPERTINO)
        self.assertTrue({'demo_amina', 'demo_daniyar', 'demo_sofia'} <= seen)

    def test_strangers_appear_only_after_the_reviewer_checks_in(self):
        report(self.reviewer, *CUPERTINO)
        self.assertNotIn('demo_alina', seen_by(self.reviewer, *CUPERTINO))
        client_for(self.reviewer).post('/api/proximity/check-in/')
        self.assertTrue({'demo_alina', 'demo_arman', 'demo_timur'} <= seen_by(self.reviewer, *CUPERTINO))

    def test_small_moves_leave_the_cast_where_it_is(self):
        report(self.reviewer, *CUPERTINO)
        amina = User.objects.get(username='demo_amina')
        report(self.reviewer, '37.33580000', '-122.00900000')  # ~100 m north
        moved = User.objects.get(username='demo_amina')
        self.assertEqual((amina.latitude, amina.longitude), (moved.latitude, moved.longitude))

    def test_expired_stories_come_back(self):
        Story.objects.update(expires_at=timezone.now() - timedelta(hours=1))
        report(self.reviewer, *CUPERTINO)
        self.assertFalse(Story.objects.filter(expires_at__lt=timezone.now()).exists())

    def test_real_location_reports_move_nobody(self):
        real = User.objects.create_user(username='real', email='real@example.com', password='x-123456')
        report(real, *CUPERTINO)
        self.assertFalse(User.objects.filter(is_review_demo=True, latitude__isnull=False).exists())

    def test_walking_in_on_aminas_zone_music_alerts_the_reviewer(self):
        song = {'id': '1', 'title': 'Song', 'artist': 'Artist', 'album': '',
                'artwork': 'https://example.com/a.jpg', 'preview_url': 'https://example.com/p.m4a'}
        with mock.patch('users.applemusic.is_configured', return_value=True), \
                mock.patch('users.applemusic.get_song', return_value=song):
            seed(zone_song_id='1')
        self.assertTrue(User.objects.get(username='demo_amina').broadcasts_zone_track)
        report(self.reviewer, *CUPERTINO)
        self.assertTrue(Notification.objects.filter(
            recipient=self.reviewer, notification_type=Notification.Type.ZONE_MUSIC,
        ).exists())

    def test_the_map_query_alone_brings_the_cast(self):
        # A phone that never stores a position still gets a populated map, and
        # the query still stores nothing about the reviewer.
        seen = seen_by(self.reviewer, *CUPERTINO)
        self.assertTrue({'demo_amina', 'demo_daniyar', 'demo_sofia'} <= seen)
        self.reviewer.refresh_from_db()
        self.assertIsNone(self.reviewer.latitude)
