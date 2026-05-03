from django.urls import re_path
from .consumers import LocationConsumer

websocket_urlpatterns = [
    re_path(r'^ws/proximity/$', LocationConsumer.as_asgi()),
]
