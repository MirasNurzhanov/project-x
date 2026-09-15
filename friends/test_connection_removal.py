from rest_framework.test import APITestCase

from friends.models import FriendRequest, HiRequest, Message
from moderation.models import ContentReport
from users.models import User


class ConnectionRemovalTests(APITestCase):
    def setUp(self):
        self.a = User.objects.create_user(username='alice', email='alice@test.example')
        self.b = User.objects.create_user(username='bob', email='bob@test.example')
        self.c = User.objects.create_user(username='other', email='other@test.example')
        HiRequest.objects.create(sender=self.a, receiver=self.b, status='accepted')
        FriendRequest.objects.create(sender=self.b, receiver=self.a, status='accepted')
        self.sent = Message.objects.create(sender=self.a, receiver=self.b, content='sent evidence')
        self.received = Message.objects.create(sender=self.b, receiver=self.a, content='received evidence')
        self.unrelated = Message.objects.create(sender=self.a, receiver=self.c, content='unrelated')
        self.report = ContentReport.objects.create(
            reporter=self.b, target_user=self.a, target_type='message',
            target_id=self.sent.pk, target_repr=self.sent.content, reason='harassment',
        )

    def remove(self, actor, other):
        self.client.force_authenticate(actor)
        return self.client.delete(f'/api/friends/{other.pk}/remove/')

    def test_removal_hides_only_initiators_copy_and_keeps_evidence(self):
        self.assertEqual(self.remove(self.a, self.b).status_code, 204)
        self.sent.refresh_from_db()
        self.received.refresh_from_db()
        self.unrelated.refresh_from_db()
        self.report.refresh_from_db()
        self.assertTrue(self.sent.deleted_for_sender)
        self.assertFalse(self.sent.deleted_for_receiver)
        self.assertFalse(self.received.deleted_for_sender)
        self.assertTrue(self.received.deleted_for_receiver)
        self.assertFalse(self.unrelated.deleted_for_sender)
        self.assertEqual(self.report.target_repr, 'sent evidence')
        self.assertEqual(Message.objects.count(), 3)
        self.assertFalse(HiRequest.objects.exists())
        self.assertFalse(FriendRequest.objects.exists())

    def test_reverse_initiator_preserves_the_other_sides_copy(self):
        self.assertEqual(self.remove(self.b, self.a).status_code, 204)
        self.sent.refresh_from_db()
        self.received.refresh_from_db()
        self.assertFalse(self.sent.deleted_for_sender)
        self.assertTrue(self.sent.deleted_for_receiver)
        self.assertTrue(self.received.deleted_for_sender)
        self.assertFalse(self.received.deleted_for_receiver)

    def test_other_user_can_find_and_read_archive_but_cannot_send(self):
        self.remove(self.a, self.b)
        self.client.force_authenticate(self.b)
        response = self.client.get('/api/friends/list/')
        self.assertEqual([row['id'] for row in response.data['archived']], [self.a.pk])
        response = self.client.get(f'/api/friends/messages/{self.a.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data['results']], [self.sent.pk, self.received.pk])
        self.assertFalse(response.data['can_send'])
        self.assertEqual(self.client.post(f'/api/friends/messages/{self.a.pk}/', {'content': 'new'}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/friends/messages/{self.a.pk}/read/',
                                         {'through_id': self.sent.pk}).status_code, 200)

    def test_removed_history_does_not_reappear_after_reconnecting(self):
        self.remove(self.a, self.b)
        HiRequest.objects.create(sender=self.a, receiver=self.b, status='accepted')
        response = self.client.get(f'/api/friends/messages/{self.b.pk}/')
        self.assertEqual(response.data['results'], [])
        self.assertTrue(response.data['can_send'])
        self.client.force_authenticate(self.b)
        self.assertEqual(len(self.client.get(f'/api/friends/messages/{self.a.pk}/').data['results']), 2)

    def test_missing_connection_does_not_mutate_messages(self):
        self.assertEqual(self.remove(self.a, self.c).status_code, 404)
        self.unrelated.refresh_from_db()
        self.assertFalse(self.unrelated.deleted_for_sender)

    def test_blocked_history_remains_hidden(self):
        from friends.models import BlockedUser
        self.remove(self.a, self.b)
        BlockedUser.objects.create(blocker=self.b, blocked=self.a)
        self.client.force_authenticate(self.b)
        self.assertEqual(self.client.get('/api/friends/list/').data['archived'], [])
        self.assertEqual(self.client.get(f'/api/friends/messages/{self.a.pk}/').status_code, 403)

    def test_removal_during_screening_prevents_a_late_message(self):
        from unittest.mock import patch
        from moderation.services import screen

        def remove_while_screening(*args, **kwargs):
            self.remove(self.a, self.b)
            return screen(*args, **kwargs)

        self.client.force_authenticate(self.a)
        with patch('friends.views.moderation_services.screen', side_effect=remove_while_screening):
            response = self.client.post(f'/api/friends/messages/{self.b.pk}/', {'content': 'late'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Message.objects.filter(content='late').exists())
