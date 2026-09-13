"""Account standing is checked against the database, never a socket's stale user."""
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


def account_can_use_socket(user_id):
    from users.models import User

    user = User.objects.filter(pk=user_id).only(
        'is_active', 'suspended_at', 'suspended_until', 'suspension_reason',
    ).first()
    return user is not None and user.is_active and user.suspension_detail() is None


class SuspensionAwareConsumer(AsyncWebsocketConsumer):
    async def dispatch(self, message):
        # Includes outgoing channel events: a suspended recipient must not keep
        # receiving a conversation. Disconnect still runs normal group cleanup.
        if message['type'] != 'websocket.disconnect':
            user = self.scope.get('user')
            if (not user or not user.is_authenticated
                    or not await database_sync_to_async(account_can_use_socket)(user.pk)):
                await self.close(code=4001)
                return
        await super().dispatch(message)
