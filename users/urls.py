from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .views import RegisterView, LogoutView, ProfileView, PrivacySettingsView, PublicProfileView, ZoneView, ZonePresetsView

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', TokenObtainPairView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('privacy/', PrivacySettingsView.as_view(), name='privacy'),
    path('zone/presets/', ZonePresetsView.as_view(), name='zone-presets'),
    path('zone/', ZoneView.as_view(), name='zone'),
    path('<int:user_id>/profile/', PublicProfileView.as_view(), name='public-profile'),
    path('profile/<int:user_id>/', PublicProfileView.as_view(), name='public-profile-v2'),
]