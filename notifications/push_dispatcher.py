"""Handing push delivery to the optional Go dispatcher.

Django's fallback executor is bounded. The dispatcher adds rate limiting,
transient retries and execution independent of web-process recycling.
This module is its client: Django keeps
every decision about *who* gets a notification — preferences, blocks,
cooldowns, all of `notifications.utils` — and hands over only the finished
envelopes.

That boundary is the reason this is worth doing at all. If the Go service knew
what a club or a Swift was, it would need the database, the models and half the
business rules, and you would be maintaining the same logic twice in two
languages. It knows about none of them: it takes a list of Expo envelopes and
gets them delivered.

Off by default. `PUSH_DISPATCHER_URL` unset means `push._queue` behaves exactly
as it does today, so this can land, be deployed, and be turned on later.
"""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

#: How long to wait on the dispatcher. Deliberately tiny — the whole promise of
#: the Go service is that it answers as soon as it has queued the work, so
#: anything beyond a second means it is saturated or gone, and in both cases
#: falling back is better than waiting.
TIMEOUT_SECONDS = 2.0
logger = logging.getLogger(__name__)


class DispatcherUnavailable(Exception):
    """The Go service could not take the work.

    Raised rather than swallowed so `send()` can decide what to do about it —
    which is to fall back to the in-process sender. A push that quietly
    disappears because a sidecar was restarting is worse than one that goes out
    the slow way.
    """


def configured() -> bool:
    """Whether the dispatcher is switched on for this deployment."""
    return bool(getattr(settings, 'PUSH_DISPATCHER_URL', ''))


def _request(messages):
    """POST the batch. Returns the parsed body, or raises DispatcherUnavailable.

    `urllib` rather than `requests`, for one reason: this is the only call in
    the project that has to work when the thing it depends on is *unhealthy*,
    and the fewer layers between the socket and the fallback the better. It is
    also one less place for a connection pool to hold state across a fork.
    """
    url = settings.PUSH_DISPATCHER_URL
    body = json.dumps({'messages': messages}).encode()

    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Content-Type': 'application/json',
            # The shared secret. Matches PUSH_AUTH_TOKEN in the Go service's
            # environment; both read it from the same Kubernetes Secret.
            'Authorization': f'Bearer {getattr(settings, "PUSH_DISPATCHER_TOKEN", "")}',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read(1 << 20) or b'{}')
    except urllib.error.HTTPError as exc:
        # 503 is the dispatcher shedding load — its queue is full. That is a
        # real answer and the right response is to send it ourselves, not to
        # retry into a service that just said it has no room.
        raise DispatcherUnavailable(f'dispatcher returned {exc.code}') from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DispatcherUnavailable(str(exc)) from exc


def send(messages) -> bool:
    """Hand `messages` to the Go dispatcher. Returns whether it took them.

    False means the caller should fall back. It never raises: a push is not
    worth failing a request over, which is the same contract `push._post`
    already holds itself to.
    """
    if not messages or not configured():
        return False
    try:
        result = _request(messages)
    except (DispatcherUnavailable, ValueError, TypeError) as exc:
        logger.warning('push_dispatcher_unavailable', extra={'error_type': type(exc).__name__})
        return False

    if not isinstance(result, dict) or not isinstance(result.get('queued'), int):
        logger.warning('push_dispatcher_response_invalid')
        return False
    rejected = result.get('rejected') or 0
    if rejected:
        # The dispatcher validated the tokens and refused some. Worth a line,
        # because a rising count here means `users.Device` is accumulating dead
        # rows that nothing is deleting.
        logger.warning('push_dispatcher_rejected', extra={'count': rejected})
    return True


def send_async(messages) -> None:
    """Compatibility entry point using the bounded transport, never a new thread.

    Dispatcher handoff can take up to TIMEOUT_SECONDS; fallback delivery runs
    off-thread. The historical name is retained for existing callers.
    """
    from .push import _queue
    _queue(messages)
