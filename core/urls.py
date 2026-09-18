"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from proximity.urls import map_urlpatterns
from .socket_tickets import SocketTicketView
from notifications.dead_tokens import DeadPushTokensView

# Admin branding. The wordmark itself is drawn by
# templates/admin/base_site.html; these are the strings Django puts in the
# <title>, in the dashboard heading, and next to the mark.
admin.site.site_header = "Admin"
admin.site.site_title = "Approx admin"
admin.site.index_title = "Overview"

urlpatterns = [
    path('api/internal/push/dead-tokens/', DeadPushTokensView.as_view(), name='dead-push-tokens'),
    path('api/socket-ticket/', SocketTicketView.as_view(), name='socket-ticket'),
    path("admin/", admin.site.urls),
    # HTML, not API: the Terms, Community Guidelines and Privacy pages the
    # app links from its sign-up gate. Served from here because Approx has no
    # marketing domain, and a legal document behind a URL that does not
    # resolve is the same as not having one. See moderation/legal_views.py.
    path("legal/", include("moderation.legal_urls")),
    path("api/users/", include("users.urls")),
    path("api/interests/", include("interests.urls")),
    path("api/proximity/", include("proximity.urls")),
    path("api/map/", include(map_urlpatterns)),
    path("api/regions/", include("regions.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api/encounters/", include("encounters.urls")),
    path("api/clubs/", include("clubs.urls")),
    path("api/venues/", include("venues.urls")),
    path("api/friends/", include("friends.urls")),
    path("api/stories/", include("stories.urls")),
    path("api/moderation/", include("moderation.urls")),
    path("api/swifts/", include("swifts.urls")),
    # The recommender rides the same prefix as the feed it ranks — see recs/urls.
    path("api/swifts/", include("recs.urls")),
    # Machine-to-machine only: the Go moderation worker in
    # services/go-moderation. Gated by a shared secret, never a user session —
    # see swifts/worker_views.py. Kept under its own prefix so it is obvious
    # from this file that nothing here is part of the app's public surface.
    path("api/internal/", include("swifts.worker_urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
