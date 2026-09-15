"""M2: legacy URLs remain readable, never writable through either transport."""
from io import BytesIO
from uuid import uuid4

from PIL import Image
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APITestCase

from clubs.models import Club, ClubMember, ClubMessage
from clubs.routing import websocket_urlpatterns as club_routes
from core.middleware import SocketTicketMiddleware
from core.socket_tickets import issue_ticket
from friends.models import HiRequest, Message
from friends.routing import websocket_urlpatterns as friend_routes
from users.models import User


def setup_conversations(test):
    test.user = User.objects.create_user(username='media', email='media@example.test')
    test.peer = User.objects.create_user(username='reader', email='reader@example.test')
    HiRequest.objects.create(sender=test.user, receiver=test.peer, status='accepted')
    test.club = Club.objects.create(name='Media', creator=test.user)
    ClubMember.objects.create(club=test.club, user=test.user)


def image_upload():
    buffer = BytesIO()
    Image.new('RGB', (2, 2), color='red').save(buffer, format='PNG')
    return SimpleUploadedFile('photo.png', buffer.getvalue(), content_type='image/png')


class MessageMediaWriteTests(APITestCase):
    def setUp(self):
        setup_conversations(self)
        self.client.force_authenticate(self.user)
        self.dm_url = f'/api/friends/messages/{self.peer.pk}/'
        self.club_url = f'/api/clubs/{self.club.pk}/messages/'

    def test_urls_are_rejected_even_with_valid_text_or_empty_values(self):
        for endpoint in (self.dm_url, self.club_url):
            for value in ('https://outside.example/file.html', '', None, [], {}):
                with self.subTest(endpoint=endpoint, value=value):
                    response = self.client.post(endpoint, {'content': 'hello', 'image_url': value}, format='json')
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('image_url', response.data)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ClubMessage.objects.exists())

    def test_urls_are_rejected_with_a_valid_file_as_well(self):
        for endpoint in (self.dm_url, self.club_url):
            response = self.client.post(endpoint, {'image': image_upload(), 'image_url': 'https://outside.example/photo.png'})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ClubMessage.objects.exists())

    def test_idempotent_replay_does_not_bypass_url_rejection(self):
        client_id = uuid4()
        Message.objects.create(sender=self.user, receiver=self.peer, content='existing', client_id=client_id)
        response = self.client.post(self.dm_url, {'client_id': str(client_id), 'image_url': 'https://outside.example/x'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.objects.count(), 1)

    def test_supported_multipart_uploads_still_work(self):
        for endpoint in (self.dm_url, self.club_url):
            response = self.client.post(endpoint, {'image': image_upload()})
            self.assertEqual(response.status_code, 201)
            self.assertTrue(response.data['image'])
        self.assertEqual(Message.objects.get().image_url, '')
        self.assertEqual(ClubMessage.objects.get().image_url, '')

    def test_legacy_images_remain_in_history_payloads(self):
        url = 'https://legacy.example/existing.jpg'
        Message.objects.create(sender=self.user, receiver=self.peer, image_url=url)
        ClubMessage.objects.create(club=self.club, sender=self.user, image_url=url)
        self.assertEqual(self.client.get(self.dm_url).data['results'][0]['image'], url)
        self.assertEqual(self.client.get(self.club_url).data[0]['image'], url)

    def test_active_content_disguised_as_images_is_rejected(self):
        for endpoint in (self.dm_url, self.club_url):
            response = self.client.post(endpoint, {'image': SimpleUploadedFile('photo.jpg', b'<html>active</html>', content_type='image/jpeg')})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(ClubMessage.objects.exists())


@override_settings(DEBUG=True)
class MessageMediaSocketTests(TransactionTestCase):
    def setUp(self):
        setup_conversations(self)

    async def test_sockets_refuse_url_fields_and_still_accept_text(self):
        app = SocketTicketMiddleware(URLRouter(friend_routes + club_routes))
        for path in (f'/ws/chat/{self.peer.pk}/', f'/ws/clubs/{self.club.pk}/'):
            ticket = issue_ticket(self.user.pk, path)
            socket = WebsocketCommunicator(app, f'{path}?ticket={ticket}')
            self.assertTrue((await socket.connect())[0])
            for value in ('https://outside.example/photo', '', None):
                await socket.send_json_to({'content': 'hello', 'image_url': value})
                self.assertEqual((await socket.receive_json_from())['type'], 'error')
            await socket.send_json_to({'content': 'hello'})
            self.assertIn((await socket.receive_json_from())['type'], ('chat_message', 'club_message'))
            await socket.disconnect()
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 1)
        self.assertEqual(await database_sync_to_async(ClubMessage.objects.count)(), 1)
