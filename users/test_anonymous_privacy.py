"""Anonymous profiles and Ghost Mode must hold against direct API requests."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from encounters.models import Encounter
from friends.models import FriendRequest
from notifications.models import Notification
from proximity.serializers import NearbyUserSerializer
from stories.models import Story
from stories import visibility as story_visibility
from swifts.models import SwiftVideo, SwiftView
from swifts.testing import make_swift
from swifts import visibility as swift_visibility
from users.models import User
from users import visibility
from users.testing import share_location


class AnonymousPrivacyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.author = self.make_user('author')
        self.friend = self.make_user('friend')
        self.stranger = self.make_user('stranger')
        FriendRequest.objects.create(
            sender=self.author, receiver=self.friend,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.swift = make_swift(self.author, located=True, shared=True)
        self.story = Story.objects.create(
            user=self.author, content='A workspace', place_name='Home',
            latitude='10.0001', longitude='10.0001',
            expires_at=timezone.now() + timedelta(hours=5),
        )

    def make_user(self, name):
        return share_location(User.objects.create_user(
            username=name, email=f'{name}@example.com', password='testpass-123',
            latitude='10.0001', longitude='10.0001', bio='Private details',
            last_seen=timezone.now(),
        ))

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def ghost(self, user, enabled=True):
        response = self.client_for(user).patch(
            '/api/users/privacy/', {'ghost_mode': enabled}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()

    def test_ghost_profile_hides_details_from_friends_and_strangers(self):
        self.ghost(self.author)
        for viewer in (self.friend, self.stranger):
            with self.subTest(viewer=viewer.username):
                response = self.client_for(viewer).get(
                    f'/api/users/profile/{self.author.pk}/',
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), {
                    'id', 'username', 'profile_picture', 'is_private',
                    'relationship', 'can_view_swifts',
                })
                self.assertFalse(response.json()['can_view_swifts'])
                self.assertFalse(visibility.can_view_profile_details(viewer, self.author))
                for field in visibility.FIELD_SETTINGS:
                    self.assertFalse(visibility.can_view(viewer, self.author, field))

    def test_owner_keeps_profile_and_own_swifts_while_ghosted(self):
        self.ghost(self.author)
        client = self.client_for(self.author)
        profile = client.get(f'/api/users/profile/{self.author.pk}/').json()
        self.assertEqual(profile['bio'], self.author.bio)
        self.assertTrue(profile['can_view_swifts'])
        self.assertTrue(swift_visibility.can_watch(self.author, self.swift))
        self.assertIn(self.swift.pk, {
            item['id'] for item in client.get(reverse('swift-mine')).json()['results']
        })

    def test_private_account_approval_is_preserved_without_ghost_mode(self):
        self.author.is_private = True
        self.author.save(update_fields=['is_private'])
        self.assertTrue(visibility.can_view_profile_details(self.friend, self.author))
        self.assertFalse(visibility.can_view_profile_details(self.stranger, self.author))
        self.assertTrue(swift_visibility.can_watch(self.friend, self.swift))
        self.assertFalse(swift_visibility.can_watch(self.stranger, self.swift))

    def test_ghost_swift_retracts_from_feed_detail_and_interactions(self):
        self.ghost(self.author)
        for viewer in (self.friend, self.stranger):
            with self.subTest(viewer=viewer.username):
                client = self.client_for(viewer)
                self.assertFalse(swift_visibility.can_watch(viewer, self.swift))
                self.assertFalse(swift_visibility.watchable(viewer).filter(pk=self.swift.pk).exists())
                feed = client.get(reverse('swift-feed'), {'chrono': '1'}).json()
                self.assertNotIn(self.swift.pk, {row['id'] for row in feed['results']})
                self.assertEqual(client.get(reverse('swift-detail', args=[self.swift.pk])).status_code, 404)
                for endpoint in ('swift-like', 'swift-repost', 'swift-view'):
                    self.assertEqual(client.post(reverse(endpoint, args=[self.swift.pk])).status_code, 404)

    def test_cached_ranked_feed_rechecks_anonymous_author(self):
        self.ghost(self.author)
        cached = SimpleNamespace(
            swift_ids=[self.swift.pk], cursor=None, has_more=False, sources_by_id={},
        )
        with patch('recs.pipeline.page', return_value=cached):
            response = self.client_for(self.friend).get(reverse('swift-feed'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    def test_leaving_ghost_restores_only_approved_content(self):
        pending = make_swift(self.author, moderation=SwiftVideo.Moderation.QUARANTINED)
        self.ghost(self.author)
        self.ghost(self.author, False)
        rows = set(swift_visibility.watchable(self.friend).values_list('pk', flat=True))
        self.assertIn(self.swift.pk, rows)
        self.assertNotIn(pending.pk, rows)

    def test_ghost_author_stories_are_hidden_from_social_feed(self):
        self.ghost(self.author)
        self.assertFalse(story_visibility.can_read(self.friend, self.story))
        self.assertFalse(Story.objects.filter(story_visibility.readable_q(self.friend)).exists())
        response = self.client_for(self.friend).get('/api/stories/friends/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.story.pk, {row['id'] for row in response.json()['results']})

    def test_both_private_map_routes_hide_everyone_from_ghost_viewer(self):
        self.ghost(self.friend)
        client = self.client_for(self.friend)
        coordinate = {'latitude': '10.0001', 'longitude': '10.0001'}
        nearby = client.post('/api/proximity/nearby/query/', coordinate, format='json')
        self.assertEqual(nearby.status_code, 200)
        self.assertEqual(nearby.json(), [])
        with patch('proximity.views._transient_place', return_value={}):
            composite = client.post('/api/map/nearby/query/', coordinate, format='json')
        self.assertEqual(composite.status_code, 200)
        self.assertEqual(composite.json()['users'], [])
        self.assertEqual(composite.json()['stories'], [])
        self.assertFalse(story_visibility.is_placed_for(self.friend, self.story))
        profile = client.get(f'/api/users/profile/{self.author.pk}/').json()
        self.assertEqual(profile['stories'][0]['place_name'], '')

    def test_serializing_unfiltered_people_cannot_bypass_mutual_ghost_rule(self):
        self.ghost(self.friend)
        rows = NearbyUserSerializer(
            [self.author], many=True,
            context={'request': SimpleNamespace(user=self.friend)},
        ).data
        self.assertEqual(rows, [])

    def test_ghost_viewer_cannot_reveal_a_swift_location(self):
        SwiftView.objects.create(swift=self.swift, viewer=self.friend)
        self.ghost(self.friend)
        self.assertTrue(swift_visibility.can_watch(self.friend, self.swift))
        self.assertFalse(swift_visibility.location_available_for(self.friend, self.swift))
        self.assertFalse(swift_visibility.can_reveal_location(self.friend, self.swift))
        detail = self.client_for(self.friend).get(reverse('swift-detail', args=[self.swift.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()['location_available'])
        self.ghost(self.friend, False)
        self.assertTrue(swift_visibility.location_available_for(self.friend, self.swift))
        self.assertTrue(swift_visibility.can_reveal_location(self.friend, self.swift))

    def test_old_encounters_and_nearby_alerts_disappear_in_both_directions(self):
        encounter = Encounter.objects.create(
            user_one=self.author, user_two=self.friend, distance_km='0.01',
        )
        for recipient, sender in ((self.friend, self.author), (self.author, self.friend)):
            Notification.objects.create(
                recipient=recipient, sender=sender,
                notification_type=Notification.Type.NEARBY_USER,
            )
        self.ghost(self.friend)
        for viewer, other in ((self.friend, self.author), (self.author, self.friend)):
            with self.subTest(viewer=viewer.username):
                self.assertFalse(visibility.can_surface_in_proximity(viewer, other))
                self.assertEqual(visibility.proximity_visible_ids(viewer, [other.pk]), set())
                client = self.client_for(viewer)
                self.assertEqual(client.get('/api/encounters/').json(), [])
                self.assertEqual(client.get('/api/notifications/').json(), [])
                response = client.patch(f'/api/encounters/{encounter.pk}/acknowledge/')
                self.assertEqual(response.status_code, 404)
