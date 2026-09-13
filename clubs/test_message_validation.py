"""M14: both club chat write paths validate through one definition, and the history cursor is parsed."""
from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from core.middleware import JWTAuthMiddleware
from users.models import User
from .models import Club, ClubMember, ClubMessage
from .routing import websocket_urlpatterns
from .serializers import MESSAGE_ID_MAX, MESSAGE_MAX_CHARS


class RestMessageValidationTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='member', email='member@example.test')
        self.club = Club.objects.create(name='Validation club', creator=self.member)
        ClubMember.objects.create(club=self.club, user=self.member, role=ClubMember.Role.OWNER)
        self.client = APIClient()
        self.client.force_authenticate(self.member)
        self.url = f'/api/clubs/{self.club.pk}/messages/'

    def post(self, body):
        return self.client.post(self.url, body, format='json')

    def test_clients_cannot_claim_a_message_type(self):
        for message_type in ('meetup_proposal', 'x' * 50, ['text'], None):
            with self.subTest(message_type=message_type):
                response = self.post({'content': 'hello', 'message_type': message_type})
                self.assertEqual(response.status_code, 400)
                self.assertIn('message_type', response.data)
                self.assertNotIn('x' * 50, response.data['detail'])
        self.assertFalse(ClubMessage.objects.exists())

        response = self.post({'content': 'hello', 'message_type': 'text'})
        self.assertEqual((response.status_code, response.data['message_type']), (201, 'text'))

    def test_content_length_is_bounded(self):
        self.assertEqual(self.post({'content': 'a' * (MESSAGE_MAX_CHARS + 1)}).status_code, 400)
        self.assertFalse(ClubMessage.objects.exists())
        self.assertEqual(self.post({'content': 'a' * MESSAGE_MAX_CHARS}).status_code, 201)

    def test_non_string_content_is_a_400_not_a_500(self):
        for content in ([1], {'text': 'hi'}, True):
            with self.subTest(content=content):
                self.assertEqual(self.post({'content': content}).status_code, 400)
        self.assertFalse(ClubMessage.objects.exists())

    def test_history_cursor_is_parsed_and_bounded(self):
        ids = [ClubMessage.objects.create(club=self.club, sender=self.member, content=str(n)).pk for n in range(3)]
        older = self.client.get(self.url, {'before': ids[2]})
        self.assertEqual((older.status_code, [m['id'] for m in older.data]), (200, ids[:2]))
        self.assertEqual(len(self.client.get(f'{self.url}?before=').data), 3)

        for cursor in ('abc', '0', '-5', '1.5', str(MESSAGE_ID_MAX + 1), '9' * 40):
            with self.subTest(cursor=cursor):
                response = self.client.get(self.url, {'before': cursor})
                self.assertEqual(response.status_code, 400)
                self.assertIn('before', response.data)


class SocketMessageValidationTests(TransactionTestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='sock', email='sock@example.test')
        self.club = Club.objects.create(name='Socket validation', creator=self.member)
        ClubMember.objects.create(club=self.club, user=self.member)
        self.application = JWTAuthMiddleware(URLRouter(websocket_urlpatterns))
        self.path = f'/ws/clubs/{self.club.pk}/?token={AccessToken.for_user(self.member)}'

    async def test_malformed_frames_are_refused_without_storing_or_closing(self):
        socket = WebsocketCommunicator(self.application, self.path)
        self.assertTrue((await socket.connect())[0])

        await socket.send_to(text_data='{not json')
        self.assertEqual((await socket.receive_json_from())['type'], 'error')

        frames = (
            [1, 2],
            'just text',
            42,
            {'content': ['not', 'a', 'string']},
            {'content': 'hello', 'message_type': 'meetup_proposal'},
            {'content': 'hello', 'message_type': 'y' * 25},
            {'content': 'a' * (MESSAGE_MAX_CHARS + 1)},
            {'content': '   '},
        )
        for index, frame in enumerate(frames):
            with self.subTest(frame=index):
                await socket.send_json_to(frame)
                reply = await socket.receive_json_from(timeout=5)
                self.assertEqual(reply['type'], 'error')
                self.assertLess(len(reply['detail']), 200)

        await socket.send_json_to({'content': 'valid', 'message_type': 'text'})
        reply = await socket.receive_json_from(timeout=5)
        self.assertEqual((reply['type'], reply['content'], reply['message_type']), ('club_message', 'valid', 'text'))
        await socket.disconnect()

        rows = await database_sync_to_async(list)(ClubMessage.objects.values_list('content', 'message_type'))
        self.assertEqual(rows, [('valid', 'text')])
