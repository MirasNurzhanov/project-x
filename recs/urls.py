"""Mounted under `/api/swifts/` alongside the feed it ranks.

Deliberately not a namespace of its own. From the client's point of view the
recommender is not a separate product — it is how the Swifts feed works — and a
`/api/recs/` prefix would suggest otherwise while making the signal endpoint
look unrelated to the feed that produced the impressions it reports on.
"""
from django.urls import path

from .views import RecsDebugView, SwiftSignalView

urlpatterns = [
    path('signals/', SwiftSignalView.as_view(), name='swift-signals'),
    path('recs/debug/', RecsDebugView.as_view(), name='swift-recs-debug'),
]
