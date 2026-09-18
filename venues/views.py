from rest_framework import generics, permissions
from rest_framework.pagination import PageNumberPagination
from .models import Venue
from .serializers import VenueSerializer


class VenuePagination(PageNumberPagination):
    page_size = 100


class VenueListView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'map_query'
    serializer_class = VenueSerializer
    pagination_class = VenuePagination
    queryset = Venue.objects.filter(is_active=True)


class VenueDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = 'map_query'
    serializer_class = VenueSerializer
    queryset = Venue.objects.filter(is_active=True)
