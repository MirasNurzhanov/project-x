"""Path-bound, single-use socket credentials. Never log tickets or cache values."""
import hashlib
import logging
import re
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger(__name__)
TICKET_TTL = 30
PATH_PATTERN = re.compile(r'/ws/(?:chat/[1-9][0-9]{0,18}|clubs/[1-9][0-9]{0,18}|notifications)/')
TICKET_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}')


class TicketUnavailable(APIException):
    status_code = 503
    default_detail = 'Secure chat authentication is temporarily unavailable.'


def _check_cache():
    # LocMem is safe only for single-process development/tests. Production
    # must share atomic add() state across every ASGI/HTTP worker.
    backend = settings.CACHES['default']['BACKEND']
    if not settings.DEBUG and backend != 'django.core.cache.backends.redis.RedisCache':
        raise TicketUnavailable()


def _ticket_key(ticket, path):
    return 'socket-ticket:' + hashlib.sha256(f'{ticket}:{path}'.encode('utf-8')).hexdigest()


def _take_ticket(key):
    if settings.CACHES['default']['BACKEND'] == 'django.core.cache.backends.redis.RedisCache':
        # Django 5.2's cache API has no atomic pop. Use the pinned backend's
        # serializer/key machinery and one primary-server Lua operation. A
        # worker crash or cache eviction cannot leave a separate replay marker
        # missing while the original credential still exists.
        redis_key = cache.make_and_validate_key(key)
        backend = cache._cache
        client = backend.get_client(redis_key, write=True)
        raw = client.eval(
            "local v = redis.call('GET', KEYS[1]); "
            "if v then redis.call('DEL', KEYS[1]) end; return v", 1, redis_key,
        )
        return None if raw is None else backend._serializer.loads(raw)
    # Local development/tests only; production is rejected by _check_cache.
    row = cache.get(key)
    if row is None or not cache.add(key + ':used', True, timeout=TICKET_TTL * 3):
        return None
    cache.delete(key)
    return row


def issue_ticket(user_id, path):
    if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
        raise ValidationError({'path': 'Choose a supported socket path.'})
    _check_cache()
    ticket = secrets.token_urlsafe(32)
    key = _ticket_key(ticket, path)
    try:
        if not cache.add(key, {
            'user_id': user_id, 'path': path, 'expires': time.time() + TICKET_TTL,
        }, timeout=TICKET_TTL):
            raise TicketUnavailable()
    except Exception:
        logger.warning('socket_ticket_issue_unavailable')
        raise TicketUnavailable() from None
    return ticket


def redeem_ticket(ticket, path):
    """Return the *current* user or None. Consumption fails closed on cache errors."""
    if not isinstance(ticket, str) or not TICKET_PATTERN.fullmatch(ticket):
        return None
    try:
        _check_cache()
        row = _take_ticket(_ticket_key(ticket, path))
        if not row or row['path'] != path or row['expires'] <= time.time():
            return None
    except Exception:
        logger.warning('socket_ticket_redemption_unavailable')
        return None

    from users.models import User
    user = User.objects.filter(pk=row['user_id']).first()
    if (user is None or not user.is_active or user.deletion_requested_at is not None
            or user.retired_account_id is not None or user.suspension_detail() is not None):
        return None
    return user


class SocketTicketThrottle(ScopedRateThrottle):
    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning('socket_ticket_throttle_unavailable')
            raise TicketUnavailable() from None


class SocketTicketView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [SocketTicketThrottle]
    throttle_scope = 'socket_ticket'

    def post(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError({'path': 'Choose a supported socket path.'})
        ticket = issue_ticket(request.user.pk, request.data.get('path'))
        return Response({'ticket': ticket, 'expires_in': TICKET_TTL}, headers={
            'Cache-Control': 'no-store', 'Pragma': 'no-cache',
        })
