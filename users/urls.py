from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView
from .views import (
    RegisterView, LogoutView, ProfileView, PrivacySettingsView, PublicProfileView,
    ProfileLinksView, ProfileLinkDetailView, UserSearchView, ZoneView,
    DeleteAccountView, MeView, SocialLoginView, SocialLinkView, SafeTokenRefreshView,
    AppleMusicSearchView, AppleMusicStatusView, ZoneAppleMusicView,
)

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', TokenObtainPairView.as_view(), name='login'),
    path('social/', SocialLoginView.as_view(), name='social-login'),
    path('social/link/', SocialLinkView.as_view(), name='social-link'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', SafeTokenRefreshView.as_view(), name='token_refresh'),
    path('me/', MeView.as_view(), name='me'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('privacy/', PrivacySettingsView.as_view(), name='privacy'),
    path('links/', ProfileLinksView.as_view(), name='profile-links'),
    path('links/<int:link_id>/', ProfileLinkDetailView.as_view(), name='profile-link-detail'),
    path('zone/apple-music/status/', AppleMusicStatusView.as_view(), name='apple-music-status'),
    path('zone/apple-music/search/', AppleMusicSearchView.as_view(), name='apple-music-search'),
    path('zone/apple-music/', ZoneAppleMusicView.as_view(), name='zone-apple-music'),
    path('zone/', ZoneView.as_view(), name='zone'),
    path('<int:user_id>/profile/', PublicProfileView.as_view(), name='public-profile'),
    path('profile/<int:user_id>/', PublicProfileView.as_view(), name='public-profile-v2'),
    path('search/', UserSearchView.as_view(), name='user-search'),
    path('delete/', DeleteAccountView.as_view(), name='delete-account'),
]