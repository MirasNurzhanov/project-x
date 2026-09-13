from urllib.parse import parse_qs
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from users.authentication import SuspensionAwareJWTAuthentication


@database_sync_to_async
def _get_user_from_token(token_str):
    try:
        auth = SuspensionAwareJWTAuthentication()
        return auth.get_user(auth.get_validated_token(token_str))
    except (TokenError, AuthenticationFailed):
        return AnonymousUser()


class JWTAuthMiddleware(BaseMiddleware):
    """
    Reads a JWT access token from the ?token= query parameter and
    populates scope['user'], mirroring what AuthMiddlewareStack does for sessions.
    """
    async def __call__(self, scope, receive, send):
        params = parse_qs(scope.get('query_string', b'').decode())
        token_list = params.get('token', [])
        scope['user'] = (
            await _get_user_from_token(token_list[0]) if token_list else AnonymousUser()
        )
        return await super().__call__(scope, receive, send)
