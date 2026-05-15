from django.urls import re_path
from .consumers import ChatConsumer, UserNotificationConsumer

websocket_urlpatterns = [
    re_path(r'^ws/chat/(?P<user_id>\d+)/$', ChatConsumer.as_asgi()),
    re_path(r'^ws/notifications/$', UserNotificationConsumer.as_asgi()),
]
