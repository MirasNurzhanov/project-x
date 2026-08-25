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

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/users/", include("users.urls")),
    path("api/interests/", include("interests.urls")),
    path("api/proximity/", include("proximity.urls")),
    path("api/map/", include(map_urlpatterns)),
    path("api/regions/", include("regions.urls")),
    path("api/notifications/", include("notifications.urls")),
    path("api/encounters/", include("encounters.urls")),
    path("api/clubs/", include("clubs.urls")),
    path("api/friends/", include("friends.urls")),
    path("api/stories/", include("stories.urls")),
    path("api/moderation/", include("moderation.urls")),
    path("api/swifts/", include("swifts.urls")),
    # The recommender rides the same prefix as the feed it ranks — see recs/urls.
    path("api/swifts/", include("recs.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)