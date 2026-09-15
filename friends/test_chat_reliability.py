from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
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

    def test_club_reconnect_pages_do_not_lose_messages_with_equal_timestamps(self):
        from clubs.models import Club, ClubMember, ClubMessage

        club = Club.objects.create(name='Catch-up', creator=self.me)
        ClubMember.objects.create(club=club, user=self.me)
        messages = [ClubMessage.objects.create(club=club, sender=self.me, content=str(n))
                    for n in range(125)]
        ClubMessage.objects.filter(club=club).update(created_at=timezone.now())
        url = f'/api/clubs/{club.pk}/messages/'
        latest = self.client.get(url)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual([row['id'] for row in latest.data], [row.pk for row in messages[-50:]])
        # A live arrival between page fetches must not move the older boundary.
        ClubMessage.objects.create(club=club, sender=self.me, content='new arrival')
        middle = self.client.get(url, {'before': latest.data[0]['id']})
        oldest = self.client.get(url, {'before': middle.data[0]['id']})
        self.assertEqual([row['id'] for row in oldest.data + middle.data + latest.data],
                         [row.pk for row in messages])

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


@override_settings(DEBUG=True)
class MessageEchoTests(TransactionTestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='echo', email='echo@example.test')
        self.receiver = User.objects.create_user(username='peer', email='peer@example.test')
        HiRequest.objects.create(sender=self.sender, receiver=self.receiver, status='accepted')

    async def test_rest_acknowledgement_and_real_socket_echo_share_the_client_key(self):
        from channels.db import database_sync_to_async
        from channels.routing import URLRouter
        from channels.testing import WebsocketCommunicator
        from core.middleware import SocketTicketMiddleware
        from core.socket_tickets import issue_ticket
        from .routing import websocket_urlpatterns

        path = f'/ws/chat/{self.receiver.pk}/'
        ticket = await database_sync_to_async(issue_ticket)(self.sender.pk, path)
        socket = WebsocketCommunicator(SocketTicketMiddleware(URLRouter(websocket_urlpatterns)),
                                       f'{path}?ticket={ticket}')
        self.assertTrue((await socket.connect())[0])
        key = str(uuid4())

        @database_sync_to_async
        def post():
            client = APIClient()
            client.force_authenticate(self.sender)
            return client.post(f'/api/friends/messages/{self.receiver.pk}/',
                               {'content': 'echo me', 'client_id': key})

        try:
            with patch('friends.views.notify_direct_message'):
                response = await post()
            echo = await socket.receive_json_from()
            self.assertEqual(response.status_code, 201)
            self.assertEqual((echo['id'], echo['client_id']), (response.data['id'], key))
            self.assertEqual(echo['content'], 'echo me')
        finally:
            await socket.disconnect()


@skipUnless(connection.vendor == 'postgresql', 'Concurrent inserts require PostgreSQL.')
class ConcurrentMessageTests(TransactionTestCase):
    def test_concurrent_retries_create_one_message_acknowledge_both_and_notify_once(self):
        sender = User.objects.create_user(username='retry', email='retry@example.test')
        receiver = User.objects.create_user(username='peer', email='peer@example.test')
        HiRequest.objects.create(sender=sender, receiver=receiver, status='accepted')
        key = str(uuid4())
        barrier = Barrier(2)

        def send():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=sender.pk))
                barrier.wait(timeout=5)
                response = client.post(f'/api/friends/messages/{receiver.pk}/',
                                       {'content': 'retry', 'client_id': key})
                return response.status_code, response.data['id'], response.data['client_id']
            finally:
                close_old_connections()

        with patch('friends.views.notify_direct_message') as notify, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(sorted(row[0] for row in results), [200, 201])
        self.assertEqual(len({row[1] for row in results}), 1)
        self.assertEqual({row[2] for row in results}, {key})
        self.assertEqual(Message.objects.count(), 1)
        notify.assert_called_once()
