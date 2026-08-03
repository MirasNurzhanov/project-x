from django.urls import path

from .views import (
    RegionDetailView,
    RegionForPointView,
    RegionSearchView,
    RegionTraceView,
)

urlpatterns = [
    path('search/', RegionSearchView.as_view(), name='region-search'),
    path('for-point/', RegionForPointView.as_view(), name='region-for-point'),
    path('trace/', RegionTraceView.as_view(), name='region-trace'),
    path('<int:pk>/', RegionDetailView.as_view(), name='region-detail'),
]
