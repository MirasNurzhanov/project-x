"""Routes for the Go moderation worker.

Its own module, and its own `/api/internal/` prefix in core/urls.py, so a
machine endpoint is never one typo away from being mounted where a phone can
reach it.
"""
from django.urls import path

from .worker_views import ModerationLeaseView, ModerationSettleView

urlpatterns = [
    path('moderation/lease/', ModerationLeaseView.as_view(), name='worker-lease'),
    path('moderation/settle/', ModerationSettleView.as_view(), name='worker-settle'),
]
