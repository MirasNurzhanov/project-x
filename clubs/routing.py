from django.urls import re_path
from .consumers import ClubChatConsumer

websocket_urlpatterns = [
    re_path(r'^ws/clubs/(?P<club_id>\d+)/$', ClubChatConsumer.as_asgi()),
]
