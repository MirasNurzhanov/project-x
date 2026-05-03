from django.urls import path
from .views import UpdateLocationView, NearbyUsersView

urlpatterns = [
    path('location/', UpdateLocationView.as_view(), name='update-location'),
    path('nearby/', NearbyUsersView.as_view(), name='nearby-users'),
]
