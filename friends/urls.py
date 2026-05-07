from django.urls import path
from .views import (
    AcceptHiView,
    BlockedUsersView,
    BlockUserView,
    FriendsListView,
    IncomingHiRequestsView,
    MessagesView,
    RemoveFriendView,
    SendHiView,
    UnblockUserView,
)

urlpatterns = [
    path('hi/', SendHiView.as_view(), name='send-hi'),
    path('hi/<int:pk>/accept/', AcceptHiView.as_view(), name='accept-hi'),
    path('requests/', IncomingHiRequestsView.as_view(), name='hi-requests'),
    path('list/', FriendsListView.as_view(), name='friends-list'),
    path('messages/<int:user_id>/', MessagesView.as_view(), name='messages'),
    path('<int:user_id>/remove/', RemoveFriendView.as_view(), name='remove-friend'),
    path('<int:user_id>/block/', BlockUserView.as_view(), name='block-user'),
    path('blocked/', BlockedUsersView.as_view(), name='blocked-users'),
    path('<int:user_id>/unblock/', UnblockUserView.as_view(), name='unblock-user'),
]
