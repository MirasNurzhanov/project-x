import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.db.models import Q


class UserNotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return
        self.group_name = f"user_{user.pk}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        pass  # client → server messages not needed

    async def new_friend(self, event):
        await self.send(json.dumps({
            'type': 'new_friend',
            'username': event['username'],
        }))


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        other_id = self.scope['url_route']['kwargs']['user_id']
        other = await self._get_user(other_id)
        if other is None:
            await self.close(code=4004)
            return

        if not await self._are_connected(user, other):
            await self.close(code=4003)
            return

        self.user = user
        self.other = other
        # Canonical group name is the same regardless of who initiates
        self.room_group = f"chat_{min(user.pk, other.pk)}_{max(user.pk, other.pk)}"

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'room_group'):
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive(self, text_data):
        try:
            content = json.loads(text_data).get('content', '').strip()
        except (ValueError, TypeError):
            await self._send_error('Invalid JSON.')
            return

        if not content:
            await self._send_error('content is required.')
            return

        # Re-check on every message: a block placed after connect must stop an
        # already-open socket, not just future connections.
        if not await self._are_connected(self.user, self.other):
            await self._send_error('You are not friends with this user.')
            await self.close(code=4003)
            return

        # The socket is the path the app actually sends on — the REST endpoint
        # is a fallback and the route photos take. Screening only the REST view
        # would therefore have filtered almost nothing, which is the shape of
        # hole that makes a content filter decorative. Same tier as the REST
        # path: a direct message is refused for the block categories and is
        # never quarantined, because it has exactly one reader who already has
        # Block, Report and Delete.
        verdict = await self._screen(content)
        if verdict.blocked:
            await self._send_error(verdict.message())
            return

        message = await self._save_message(content)
        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'chat.message',
                'id': message.id,
                'sender_id': self.user.pk,
                'content': content,
                'created_at': message.created_at.isoformat(),
            },
        )

    # Handler called by group_send (dots become underscores in method name)
    async def chat_message(self, event):
        await self.send(json.dumps({
            'type': 'chat_message',
            'id': event['id'],
            'sender_id': event['sender_id'],
            'content': event['content'],
            'image_url': event.get('image_url'),
            'created_at': event['created_at'],
        }))

    async def _send_error(self, detail):
        await self.send(json.dumps({'type': 'error', 'detail': detail}))

    @database_sync_to_async
    def _get_user(self, user_id):
        from users.models import User
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

    @database_sync_to_async
    def _are_connected(self, user_a, user_b):
        from .models import BlockedUser, HiRequest
        if BlockedUser.objects.filter(
            Q(blocker=user_a, blocked=user_b) | Q(blocker=user_b, blocked=user_a)
        ).exists():
            return False
        return HiRequest.objects.filter(
            status=HiRequest.Status.ACCEPTED,
        ).filter(
            Q(sender=user_a, receiver=user_b) | Q(sender=user_b, receiver=user_a)
        ).exists()

    @database_sync_to_async
    def _screen(self, content):
        from moderation import services as moderation_services

        return moderation_services.screen(content, raise_on_block=False)

    @database_sync_to_async
    def _save_message(self, content):
        from notifications.utils import notify_direct_message
        from .models import Message
        message = Message.objects.create(
            sender=self.user,
            receiver=self.other,
            content=content,
        )
        # The socket is the path the app actually sends on — the REST endpoint
        # is only a fallback and the route photos take. Pushing from there
        # alone meant a typed message reached nobody who did not already have
        # the conversation open. Runs here, inside the same thread-pool hop
        # that saved the row, because the Expo call is off-thread regardless
        # and the block lookup it does needs the database.
        try:
            notify_direct_message(self.user, self.other, content)
        except Exception as exc:  # noqa: BLE001 — never drop a delivered message
            print(f'[Push] dm alert failed: {exc}')
        return message
