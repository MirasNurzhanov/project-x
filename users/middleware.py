"""Presence heartbeat.

No new polling loop and no websocket: the app already talks to the server
constantly while it is open. The nav bar's badge polls run on *every*
authenticated screen (about every 5s), and the map adds its own 10s tick on top.
Marking presence from traffic that already exists costs one indexed UPDATE at
most every `WRITE_EVERY` seconds and nothing on the battery.

The one thing this must not do is count traffic from an app that is not open.
`backgroundLocation` keeps hitting the API every 30s with the app closed, so a
naive "any authenticated request means online" would leave those users pinned
online indefinitely. Presence is therefore opt-in per request: the client sets
`X-Aura-Presence: foreground`, and the background task does not.

A header is safe here because it can only ever make you *look present*, which is
the thing the user is choosing to reveal — it grants no access and reveals
nobody else's data.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .visibility import ONLINE_WINDOW

#: Throttle. Well under ONLINE_WINDOW so a user never lapses between writes,
#: while collapsing the ~12 requests a minute the app makes into one UPDATE.
WRITE_EVERY = timedelta(seconds=30)

HEADER = 'HTTP_X_AURA_PRESENCE'
FOREGROUND = 'foreground'
#: Sent on the way to the background, so someone who closes the app drops off
#: immediately instead of lingering for the rest of the window.
BACKGROUND = 'background'


class PresenceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # After the view: DRF authenticates per-view, so request.user is not
        # populated until it has run.
        try:
            self._touch(request)
        except Exception:
            # Presence is never worth failing a request over.
            pass
        return response

    def _touch(self, request):
        from .models import User

        state = request.META.get(HEADER)
        if state not in (FOREGROUND, BACKGROUND):
            return

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return

        now = timezone.now()

        if state == BACKGROUND:
            # Backdate rather than nulling: "was here a moment ago" is true and
            # still supports a "last seen" reading, whereas None would claim
            # they had never opened the app.
            User.objects.filter(pk=user.pk).update(
                last_seen=now - ONLINE_WINDOW,
                motion_class=User.MotionClass.STATIONARY,
                motion_updated_at=None,
            )
            return

        # The throttle lives in the WHERE clause rather than in a Python `if`
        # on request.user.last_seen. That attribute is only as fresh as however
        # this request happened to load the user, so testing it in Python makes
        # the write rate depend on the authentication path. One conditional
        # UPDATE is authoritative, atomic, and saves the read.
        #
        # .update() also avoids racing a concurrent full save() of the row, and
        # does not bump the auto_now `updated_at`.
        User.objects.filter(
            Q(pk=user.pk),
            Q(last_seen__isnull=True) | Q(last_seen__lte=now - WRITE_EVERY),
        ).update(last_seen=now)
