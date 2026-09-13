from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from .models import BlockedUser, HiRequest, Message


class ChatReliabilityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = User.objects.create_user(username='reader', email='reader@example.test')
        self.other = User.objects.create_user(username='peer', email='peer@example.test')
        self.stranger = User.objects.create_user(username='stranger', email='stranger@example.test')
        HiRequest.objects.create(sender=self.me, receiver=self.other, status='accepted')
        self.client = APIClient()
        self.client.force_authenticate(self.me)
        self.url = f'/api/friends/messages/{self.other.pk}/'

    def incoming(self, **fields):
        return Message.objects.create(sender=self.other, receiver=self.me, content='hello', **fields)

    def test_history_is_bounded_stable_and_read_only(self):
        messages = [self.incoming() for _ in range(65)]
        Message.objects.all().update(created_at=timezone.now() - timedelta(days=1))
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual([row['id'] for row in page.data['results']], [row.pk for row in messages[-50:]])
        self.assertTrue(page.data['has_more'])
        self.assertEqual(page.data['next_before'], messages[-50].pk)
        self.assertEqual(Message.objects.filter(is_read=False).count(), 65)
        # A new message cannot move the boundary of the older page.
        newest = self.incoming()
        older = self.client.get(self.url, {'before': page.data['next_before']})
        self.assertEqual([row['id'] for row in older.data['results']], [row.pk for row in messages[:15]])
        self.assertFalse(older.data['has_more'])
        self.assertIsNone(older.data['next_before'])
        self.assertNotIn(newest.pk, [row['id'] for row in older.data['results']])

    def test_history_rejects_invalid_cursors_and_limits(self):
        for params in ({'before': '-1'}, {'before': 'bad'}, {'before': str(2**100)},
                       {'before': '0'}, {'limit': 0}, {'limit': 101}, {'limit': 'bad'}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params).status_code, 400)

    def test_read_acknowledges_only_visible_inbound_messages_through_its_anchor(self):
        older, anchor, newer = [self.incoming() for _ in range(3)]
        hidden = self.incoming(deleted_for_receiver=True)
        outgoing = Message.objects.create(sender=self.me, receiver=self.other, content='out')
        response = self.client.post(self.url + 'read/', {'through_id': anchor.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['updated'], 2)
        self.assertEqual(set(Message.objects.filter(is_read=True).values_list('id', flat=True)), {older.pk, anchor.pk})
        self.assertEqual(self.client.post(self.url + 'read/', {'through_id': anchor.pk}).data['updated'], 0)
        for bad_anchor in (hidden.pk, outgoing.pk, 999999):
            self.assertEqual(self.client.post(self.url + 'read/', {'through_id': bad_anchor}).status_code, 404)
        newer.refresh_from_db()
        self.assertFalse(newer.is_read)

    def test_read_and_history_preserve_relationship_and_deletion_gates(self):
        visible = self.incoming()
        self.incoming(deleted_for_receiver=True)
        stranger_message = Message.objects.create(sender=self.stranger, receiver=self.me, content='other thread')
        self.assertEqual([row['id'] for row in self.client.get(self.url).data['results']], [visible.pk])
        self.assertEqual(self.client.post(self.url + 'read/', {'through_id': stranger_message.pk}).status_code, 404)
        for value in ({}, {'through_id': -1}, {'through_id': str(2**100)}, {'through_id': []}):
            self.assertEqual(self.client.post(self.url + 'read/', value, format='json').status_code, 400)
        BlockedUser.objects.create(blocker=self.other, blocked=self.me)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url + 'read/', {'through_id': visible.pk}).status_code, 403)

    def test_text_keys_correlate_ack_echo_and_history_and_deduplicate_retries(self):
        client_id = str(uuid4())
        with patch('friends.views.notify_direct_message') as notify, \
                patch('friends.views.get_channel_layer') as channel:
            from unittest.mock import AsyncMock
            channel.return_value.group_send = AsyncMock()
            first = self.client.post(self.url, {'content': 'same words', 'client_id': client_id})
            second = self.client.post(self.url, {'content': 'changed on retry', 'client_id': client_id})
        self.assertEqual([first.status_code, second.status_code], [201, 200])
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.data['client_id'], client_id)
        self.assertEqual(channel.return_value.group_send.call_args.args[1]['client_id'], client_id)
        notify.assert_called_once()
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(self.client.get(self.url).data['results'][0]['client_id'], client_id)

    def test_identical_text_with_distinct_keys_and_legacy_posts_remain_distinct(self):
        payloads = [{'content': 'same', 'client_id': str(uuid4())} for _ in range(2)] + [{'content': 'same'}]
        for payload in payloads:
            self.assertEqual(self.client.post(self.url, payload).status_code, 201)
        self.assertEqual(Message.objects.count(), 3)
        self.assertEqual(self.client.post(self.url, {'content': 'same', 'client_id': 'invalid'}).status_code, 400)

    def test_database_key_uniqueness_and_deleted_retry_do_not_resurrect(self):
        key = uuid4()
        message = Message.objects.create(sender=self.me, receiver=self.other, client_id=key, content='sent')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Message.objects.create(sender=self.me, receiver=self.other, client_id=key, content='again')
        message.deleted_for_sender = True
        message.save(update_fields=['deleted_for_sender'])
        self.assertEqual(self.client.post(self.url, {'client_id': str(key), 'content': 'sent'}).status_code, 404)
