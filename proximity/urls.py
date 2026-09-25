from django.urls import path
from .views import (
    CheckInView,
    CheckOutView,
    EndStaleCheckInView,
    MapQueryView,
    NearbyMapView,
    NearbyQueryView,
    NearbyUsersView,
    UpdateLocationView,
)

urlpatterns = [
    path('location/', UpdateLocationView.as_view(), name='update-location'),
    path('nearby/', NearbyUsersView.as_view(), name='nearby-users'),
    # The private browsing anchor: a coordinate used for one request and
    # never stored. See NearbyQueryView.
    path('nearby/query/', NearbyQueryView.as_view(), name='nearby-query'),
    # Visibility to non-friends, for this session only. See CheckInView.
    path('check-in/', CheckInView.as_view(), name='check-in'),
    path('check-out/', CheckOutView.as_view(), name='check-out'),
    path('check-in/end-stale/', EndStaleCheckInView.as_view(), name='check-in-end-stale'),
]

map_urlpatterns = [
    path('nearby/', NearbyMapView.as_view(), name='map-nearby'),
    path('nearby/query/', MapQueryView.as_view(), name='map-nearby-query'),
]
