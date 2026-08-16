from django.urls import path
from .views import (
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
]

map_urlpatterns = [
    path('nearby/', NearbyMapView.as_view(), name='map-nearby'),
    path('nearby/query/', MapQueryView.as_view(), name='map-nearby-query'),
]
