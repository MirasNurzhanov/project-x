from django.urls import path
from .views import (
    NotificationListView, MarkNotificationReadView, MarkAllReadView,
    ClearAllNotificationsView,
)

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification-list'),
    path('read-all/', MarkAllReadView.as_view(), name='notification-read-all'),
    path('mark-all-read/', MarkAllReadView.as_view(), name='notification-mark-all-read'),
    path('clear-all/', ClearAllNotificationsView.as_view(), name='notification-clear-all'),
    path('<int:pk>/read/', MarkNotificationReadView.as_view(), name='notification-read'),
]
