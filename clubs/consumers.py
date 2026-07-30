import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


class ClubChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        self.club_id = self.scope['url_route']['kwargs']['club_id']

        if not await self._is_member(user, self.club_id):
            await self.close(code=4003)
            return

        self.user = user
        self.room_group = f'club_{self.club_id}'

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, 'room_group'):
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            await self._send_error('Invalid JSON.')
            return

        content = data.get('content', '').strip()
        message_type = data.get('message_type', 'text')

        if not content:
            await self._send_error('content is required.')
            return

        # Re-verify membership on every message so a kicked user's open socket
        # stops working immediately.
        if not await self._is_member(self.user, self.club_id):
            await self._send_error('You are no longer a member of this club.')
            await self.close(code=4003)
            return

        message = await self._save_message(content, message_type)
        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'club.message',
                'id': message.id,
                'sender_id': self.user.pk,
                'sender_username': self.user.username,
                'content': content,
                'message_type': message_type,
                'image_url': message.image.url if message.image else None,
                'created_at': message.created_at.isoformat(),
            },
        )

    async def club_message(self, event):
        await self.send(json.dumps({
            'type': 'club_message',
            'id': event['id'],
            'sender_id': event['sender_id'],
            'sender_username': event['sender_username'],
            'content': event['content'],
            'message_type': event['message_type'],
            'image_url': event.get('image_url'),
            'created_at': event['created_at'],
        }))

    async def _send_error(self, detail):
        await self.send(json.dumps({'type': 'error', 'detail': detail}))

    @database_sync_to_async
    def _is_member(self, user, club_id):
        from .models import ClubMember
        return ClubMember.objects.filter(club_id=club_id, user=user).exists()

    @database_sync_to_async
    def _save_message(self, content, message_type):
        from .models import ClubMessage
        return ClubMessage.objects.create(
            club_id=self.club_id,
            sender=self.user,
            content=content,
            message_type=message_type,
        )
