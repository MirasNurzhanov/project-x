from datetime import timedelta
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from clubs.models import Club, ClubMember, ClubMessage
from clubs.routing import websocket_urlpatterns as club_routes
from core.middleware import JWTAuthMiddleware
from friends.models import HiRequest, Message
from friends.routing import websocket_urlpatterns as friend_routes
from users.models import User


class SocketSuspensionTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='socket', email='socket@example.test')
        self.other = User.objects.create_user(username='peer', email='peer@example.test')
        HiRequest.objects.create(sender=self.user, receiver=self.other, status='accepted')
        self.club = Club.objects.create(name='Socket club', creator=self.user)
        ClubMember.objects.create(club=self.club, user=self.user)
        self.token = str(AccessToken.for_user(self.user))
        self.routes = [f'/ws/chat/{self.other.pk}/', f'/ws/clubs/{self.club.pk}/', '/ws/notifications/']
        self.application = JWTAuthMiddleware(URLRouter(friend_routes + club_routes))

    def socket(self, path, token=None):
        return WebsocketCommunicator(self.application, f'{path}?token={self.token if token is None else token}')

    @database_sync_to_async
    def standing(self, **fields):
        User.objects.filter(pk=self.user.pk).update(**fields)

    async def test_bans_temporary_suspensions_and_inactive_accounts_cannot_connect(self):
        for fields in (
            {'suspended_at': timezone.now(), 'suspended_until': None},
            {'suspended_at': timezone.now(), 'suspended_until': timezone.now() + timedelta(days=1)},
            {'suspended_at': None, 'suspended_until': None, 'is_active': False},
        ):
            await self.standing(**fields)
            for path in self.routes:
                socket = self.socket(path)
                self.assertEqual(await socket.connect(), (False, 4001))
                await socket.disconnect()

    async def test_lapsed_suspension_can_connect_but_invalid_tokens_cannot(self):
        await self.standing(suspended_at=timezone.now() - timedelta(days=2),
                            suspended_until=timezone.now() - timedelta(days=1))
        for path in self.routes:
            socket = self.socket(path)
            self.assertTrue((await socket.connect())[0])
            await socket.disconnect()
            socket = self.socket(path, 'invalid')
            self.assertEqual(await socket.connect(), (False, 4001))
            await socket.disconnect()

    async def test_already_open_sockets_close_on_the_next_inbound_message(self):
        sockets = [self.socket(path) for path in self.routes]
        for socket in sockets:
            self.assertTrue((await socket.connect())[0])
        await self.standing(suspended_at=timezone.now(), suspended_until=None)
        for socket in sockets:
            await socket.send_json_to({'content': 'should never be stored'})
            self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4001})
            await socket.disconnect()
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 0)
        self.assertEqual(await database_sync_to_async(ClubMessage.objects.count)(), 0)

    async def test_suspended_recipient_cannot_receive_channel_events(self):
        socket = self.socket('/ws/notifications/')
        self.assertTrue((await socket.connect())[0])
        await self.standing(suspended_at=timezone.now(), suspended_until=None)
        await get_channel_layer().group_send(f'user_{self.user.pk}', {'type': 'new_friend', 'username': 'peer'})
        self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4001})
        await socket.disconnect()

    async def test_suspension_during_moderation_prevents_persistence(self):
        from moderation.services import screen

        for path, consumer in ((self.routes[0], 'friends.consumers.ChatConsumer'),
                               (self.routes[1], 'clubs.consumers.ClubChatConsumer')):
            await self.standing(suspended_at=None, suspended_until=None)
            socket = self.socket(path)
            self.assertTrue((await socket.connect())[0])

            async def suspend_while_screening(content):
                await self.standing(suspended_at=timezone.now(), suspended_until=None)
                return await database_sync_to_async(screen)(content, raise_on_block=False)

            with patch(f'{consumer}._screen', side_effect=suspend_while_screening):
                await socket.send_json_to({'content': 'coffee later?'})
                self.assertEqual(await socket.receive_output(), {'type': 'websocket.close', 'code': 4001})
            await socket.disconnect()
        self.assertEqual(await database_sync_to_async(Message.objects.count)(), 0)
        self.assertEqual(await database_sync_to_async(ClubMessage.objects.count)(), 0)
