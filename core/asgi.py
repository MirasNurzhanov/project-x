import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

# Must be called before any app imports so django.setup() runs first
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from core.middleware import JWTAuthMiddleware  # noqa: E402
from friends.routing import websocket_urlpatterns as friends_ws  # noqa: E402
from clubs.routing import websocket_urlpatterns as clubs_ws  # noqa: E402

# Sockets carry conversations only. Position reporting and map reads are REST,
# so `users.visibility` is reached by one path per surface rather than two —
# `ws/proximity/` was a second copy of the nearby query that predated the zone
# engine and wrote a position before checking whether sharing was on.
application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': JWTAuthMiddleware(
        URLRouter(friends_ws + clubs_ws)
    ),
})
