from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest, HiRequest, Message
from users.models import User
from .badges import navigation_badges
from .models import Notification


class NavigationBadgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.me, cls.sender, cls.other = [
            User.objects.create_user(username=name, email=f'{name}@example.com')
            for name in ('me', 'sender', 'other')
        ]

    def seed(self, sender):
        Message.objects.create(sender=sender, receiver=self.me, content='hello')
        HiRequest.objects.create(sender=sender, receiver=self.me)
        FriendRequest.objects.create(sender=sender, receiver=self.me)
        Notification.objects.create(
            sender=sender, recipient=self.me, notification_type='story_reaction',
        )

    def test_counts_in_one_query_without_loading_content(self):
        self.seed(self.sender)
        Message.objects.bulk_create([
            Message(sender=self.sender, receiver=self.me, content='private')
            for _ in range(100)
        ])
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(navigation_badges(self.me), {'chat': 101, 'alerts': 3})
        self.assertEqual(len(queries), 1)
        self.assertNotIn('"content"', queries[0]['sql'])

    def test_excludes_read_deleted_accepted_other_accounts_and_proximity(self):
        self.seed(self.sender)
        Message.objects.filter(receiver=self.me).update(deleted_for_receiver=True)
        Message.objects.create(sender=self.sender, receiver=self.me, is_read=True)
        Message.objects.create(sender=self.me, receiver=self.other)
        HiRequest.objects.filter(receiver=self.me).update(status='accepted')
        FriendRequest.objects.filter(receiver=self.me).update(status='rejected')
        Notification.objects.filter(recipient=self.me).update(is_read=True)
        for kind in ('nearby_user', 'proximity', 'nearby', 'wave'):
            Notification.objects.create(sender=self.sender, recipient=self.me, notification_type=kind)
        Notification.objects.create(sender=self.sender, recipient=self.other, notification_type='story_reply')
        self.assertEqual(navigation_badges(self.me), {'chat': 0, 'alerts': 0})

    def test_blocks_apply_immediately_in_both_directions(self):
        self.seed(self.sender)
        self.seed(self.other)
        BlockedUser.objects.create(blocker=self.me, blocked=self.sender)
        BlockedUser.objects.create(blocker=self.other, blocked=self.me)
        Notification.objects.create(recipient=self.me, notification_type='new_device')
        self.assertEqual(navigation_badges(self.me), {'chat': 0, 'alerts': 1})
        BlockedUser.objects.all().delete()
        self.assertEqual(navigation_badges(self.me), {'chat': 2, 'alerts': 7})

    def test_endpoint_requires_authentication_and_is_not_cacheable(self):
        client = APIClient()
        self.assertEqual(client.get('/api/notifications/badges/').status_code, 401)
        client.force_authenticate(self.me)
        response = client.get('/api/notifications/badges/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'chat': 0, 'alerts': 0})
        self.assertEqual(response['Cache-Control'], 'no-store')
