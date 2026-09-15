from urllib.parse import parse_qs
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from .socket_tickets import redeem_ticket


@database_sync_to_async
def _get_user_from_ticket(ticket, path):
    return redeem_ticket(ticket, path) or AnonymousUser()


class SocketTicketMiddleware(BaseMiddleware):
    """Redeem an opaque ticket; access/refresh JWT query authentication is refused."""
    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope['user'] = AnonymousUser()
        raw = scope.get('query_string', b'')
        try:
            params = parse_qs(raw.decode('ascii'), keep_blank_values=True, max_num_fields=2) if len(raw) <= 128 else {}
        except (UnicodeDecodeError, ValueError):
            params = {}
        if set(params) == {'ticket'} and len(params['ticket']) == 1:
            scope['user'] = await _get_user_from_ticket(params['ticket'][0], scope.get('path', ''))
        return await super().__call__(scope, receive, send)
