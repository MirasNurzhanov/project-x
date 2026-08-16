from django.urls import path
from .views import (
    MapCheckInView,
    NearbyMapView,
    NearbyUsersView,
    UpdateLocationView,
)

urlpatterns = [
    path('location/', UpdateLocationView.as_view(), name='update-location'),
    # POST to go on the map, DELETE to come off, GET for the clock. The
    # only route that writes `is_checked_in`.
    path('check-in/', MapCheckInView.as_view(), name='map-check-in'),
    path('nearby/', NearbyUsersView.as_view(), name='nearby-users'),
]

map_urlpatterns = [
    path('nearby/', NearbyMapView.as_view(), name='map-nearby'),
]
