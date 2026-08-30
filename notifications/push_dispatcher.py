"""Handing push delivery to the Go dispatcher instead of a daemon thread.

`push._queue` currently starts `threading.Thread(target=_post).start()` for
every send. That works, and its docstring is honest about why it is acceptable:
nothing upstream can act on a failed push, so a dead Expo costs a log line.

What it cannot do is any of the following, and each one is a thing you only
want once volume makes it matter:

  * **Bound anything.** A thread per batch has no ceiling. A fan-out to a large
    club during an incident starts as many threads as there are batches, and
    the first symptom is the web worker's memory rather than the push.
  * **Rate-limit.** Every thread races the others to Expo independently, so the
    request rate is whatever the traffic happens to produce. Expo throttling us
    is invisible from here.
  * **Retry.** `_post` swallows the exception. A five-second Expo blip drops
    every push that overlapped it.
  * **Survive the response cycle usefully.** The thread is tied to a gunicorn
    worker that may be recycled underneath it.

`approx-push` (../approx-push) does those four things. This module is the
client for it, and it is deliberately the thinnest possible one: Django keeps
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
import threading
import urllib.error
import urllib.request

from django.conf import settings

#: How long to wait on the dispatcher. Deliberately tiny — the whole promise of
#: the Go service is that it answers as soon as it has queued the work, so
#: anything beyond a second means it is saturated or gone, and in both cases
#: falling back is better than waiting.
TIMEOUT_SECONDS = 2.0


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
            return json.loads(response.read() or b'{}')
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
    except DispatcherUnavailable as exc:
        print(f'[Push] dispatcher unavailable, falling back in-process: {exc}')
        return False

    rejected = result.get('rejected') or 0
    if rejected:
        # The dispatcher validated the tokens and refused some. Worth a line,
        # because a rising count here means `users.Device` is accumulating dead
        # rows that nothing is deleting.
        print(f'[Push] dispatcher rejected {rejected}: {result.get("errors")}')
    return True


def send_async(messages) -> None:
    """`send`, off the request cycle.

    Still a thread — but a single short-lived one making a single sub-second
    local call, rather than one per batch making HTTPS calls to a third party
    with no ceiling on how many run at once. The unbounded part has moved into
    the Go service, where it is a bounded queue with a rate limiter in front.

    A cleaner version of this would not need a thread at all: Django 5 can
    schedule work with `transaction.on_commit`, and if this ever moves onto an
    async view `httpx.AsyncClient` removes the thread entirely. Left as a
    thread here so the change to `push._queue` stays a one-line swap.
    """
    threading.Thread(target=send, args=(messages,), daemon=True).start()
