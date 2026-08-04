from django.urls import path
from .views import UpdateLocationView, NearbyUsersView, NearbyMapView

urlpatterns = [
    path('location/', UpdateLocationView.as_view(), name='update-location'),
    path('nearby/', NearbyUsersView.as_view(), name='nearby-users'),
]

map_urlpatterns = [
    path('nearby/', NearbyMapView.as_view(), name='map-nearby'),
]
