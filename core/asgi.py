import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

# Must be called before any app imports so django.setup() runs first
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402
from core.middleware import JWTAuthMiddleware  # noqa: E402
from friends.routing import websocket_urlpatterns as friends_ws  # noqa: E402
from clubs.routing import websocket_urlpatterns as clubs_ws  # noqa: E402

# ── Static files ──────────────────────────────────────────────────────────────
# Nothing else in this deployment serves /static/. Railway runs uvicorn against
# this module directly — no nginx in front, no whitenoise in the middleware and
# no collectstatic step — so before this wrapper every request for
# admin/css/base.css returned 404 and the Django admin rendered as unstyled
# HTML in production. (It looked fine locally only because runserver installs
# this same handler for you when DEBUG is on.)
#
# ASGIStaticFilesHandler resolves through the staticfiles *finders*, so it
# serves straight out of the app directories and STATICFILES_DIRS with no build
# step, and it calls the static view with insecure=True — meaning it works with
# DEBUG off, which `django.conf.urls.static.static()` does not.
#
# It is a per-file filesystem read on a thread, which is the wrong tool for a
# high-traffic asset. It is the right one here: the only static files this
# project has are the admin's own, behind a staff login, on a handful of
# requests a day. Put a CDN or whitenoise in front the moment that stops being
# true.

# Sockets carry conversations only. Position reporting and map reads are REST,
# so `users.visibility` is reached by one path per surface rather than two —
# `ws/proximity/` was a second copy of the nearby query that predated the zone
# engine and wrote a position before checking whether sharing was on.
application = ProtocolTypeRouter({
    'http': ASGIStaticFilesHandler(django_asgi_app),
    'websocket': JWTAuthMiddleware(
        URLRouter(friends_ws + clubs_ws)
    ),
})
