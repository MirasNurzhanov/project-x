"""Request-local IDs work across Django's synchronous and ASGI call chains."""
import logging
import time
import uuid

from asgiref.sync import iscoroutinefunction
from django.utils.decorators import sync_and_async_middleware

from .structured_logging import request_id

logger = logging.getLogger(__name__)


def completed(response, started):
    response['X-Request-ID'] = request_id.get()
    logger.info('request_completed', extra={
        'status_code': response.status_code,
        'duration_ms': round((time.monotonic() - started) * 1000, 2),
    })
    return response


@sync_and_async_middleware
def RequestLoggingMiddleware(get_response):
    # Generate our own ID: untrusted headers cannot inject log data or cause
    # two unrelated requests to share a chosen identifier.
    if iscoroutinefunction(get_response):
        async def middleware(request):
            token = request_id.set(uuid.uuid4().hex)
            request.aura_request_id = request_id.get()
            started = time.monotonic()
            try:
                return completed(await get_response(request), started)
            finally:
                request_id.reset(token)
    else:
        def middleware(request):
            token = request_id.set(uuid.uuid4().hex)
            request.aura_request_id = request_id.get()
            started = time.monotonic()
            try:
                return completed(get_response(request), started)
            finally:
                request_id.reset(token)
    return middleware
