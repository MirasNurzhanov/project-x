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

        # See the note in friends/consumers: the socket is the real send path,
        # so screening the REST view alone would filter almost nothing. A club
        # is semi-public, so unlike a DM its flagged messages are quarantined
        # rather than delivered — `_save_message` stores the verdict.
        verdict = await self._screen(content)
        if verdict.blocked:
            await self._send_error(verdict.message())
            return

        message = await self._save_message(content, message_type, verdict)
        if verdict.flagged:
            # Stored, invisible to the club, and in the moderation queue. It is
            # deliberately not broadcast: putting it on everyone's screen and
            # then hiding it on the next fetch would be worse than either.
            return
        message_data = await self._serialize_message(message.pk)
        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'club.message',
                'message_data': message_data,
            },
        )

    async def club_message(self, event):
        await self.send(json.dumps({
            'type': 'club_message',
            **event['message_data'],
        }))

    async def club_event_status(self, event):
        await self.send(json.dumps({
            'type': 'club_event_status',
            'event_id': event['event_id'],
            'status': event['status'],
            'approved_at': event.get('approved_at'),
            'expires_at': event.get('expires_at'),
            'proposal': event.get('proposal'),
        }))

    async def _send_error(self, detail):
        await self.send(json.dumps({'type': 'error', 'detail': detail}))

    @database_sync_to_async
    def _is_member(self, user, club_id):
        from .models import ClubMember
        return ClubMember.objects.filter(club_id=club_id, user=user).exists()

    @database_sync_to_async
    def _screen(self, content):
        from moderation import services as moderation_services

        return moderation_services.screen(content, raise_on_block=False)

    @database_sync_to_async
    def _save_message(self, content, message_type, verdict=None):
        from moderation import services as moderation_services
        from moderation.models import ContentReport
        from notifications.utils import notify_club_message
        from .models import ClubMessage

        moderation = (
            moderation_services.moderation_for(verdict)
            if verdict is not None
            else ClubMessage._meta.get_field('moderation').default
        )
        message = ClubMessage.objects.create(
            club_id=self.club_id,
            sender=self.user,
            content=content,
            message_type=message_type,
            moderation=moderation,
        )
        if verdict is not None and verdict.flagged:
            moderation_services.record_automated_flag(
                message, ContentReport.Target.CLUB_MESSAGE, verdict, author=self.user,
            )
            # No push for a message nobody in the club can see.
            return message
        # Same reason as the DM socket: this is the path the app sends on, so
        # without this a club could hold an entire conversation that no member
        # with the app closed ever heard about. `notify_club_message` owns the
        # per-member cooldown that stops a busy club buzzing everyone's phone
        # on every line.
        try:
            notify_club_message(message.club, self.user, content)
        except Exception as exc:  # noqa: BLE001 — never drop a delivered message
            print(f'[Push] club message alert failed: {exc}')
        return message

    @database_sync_to_async
    def _serialize_message(self, message_pk):
        from .models import ClubMessage
        from .serializers import ClubMessageSerializer
        msg = ClubMessage.objects.select_related(
            'sender', 'event__created_by', 'event__approved_by',
        ).get(pk=message_pk)
        return dict(ClubMessageSerializer(msg).data)
