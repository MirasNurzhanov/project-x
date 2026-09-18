from django.urls import path
from .views import VenueDetailView, VenueListView

urlpatterns = [
    path('', VenueListView.as_view(), name='venue-list'),
    path('<int:pk>/', VenueDetailView.as_view(), name='venue-detail'),
]
