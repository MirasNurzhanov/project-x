from django.urls import path
from .views import (
    AcceptHiView,
    FriendsListView,
    IncomingHiRequestsView,
    MessagesView,
    SendHiView,
)

urlpatterns = [
    path('hi/', SendHiView.as_view(), name='send-hi'),
    path('hi/<int:pk>/accept/', AcceptHiView.as_view(), name='accept-hi'),
    path('requests/', IncomingHiRequestsView.as_view(), name='hi-requests'),
    path('list/', FriendsListView.as_view(), name='friends-list'),
    path('messages/<int:user_id>/', MessagesView.as_view(), name='messages'),
]
