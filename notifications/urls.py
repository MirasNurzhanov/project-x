from django.urls import path
from .views import NotificationListView, MarkNotificationReadView, MarkAllReadView

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification-list'),
    path('read-all/', MarkAllReadView.as_view(), name='notification-read-all'),
    path('<int:pk>/read/', MarkNotificationReadView.as_view(), name='notification-read'),
]
