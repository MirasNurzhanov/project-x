from django.urls import path
from .views import (
    CreateStoryView,
    DeleteStoryView,
    FriendStoriesView,
    MyStoriesView,
    NearbyStoriesView,
    ReactToStoryView,
    StarredStoriesView,
    StarStoryView,
)

urlpatterns = [
    path('', CreateStoryView.as_view(), name='story-create'),
    # Two feeds, two rules. `nearby/` is the map's - it needs a position and
    # only ever answers with pins. `friends/` is the social one and needs
    # neither. See stories/visibility.py.
    path('nearby/', NearbyStoriesView.as_view(), name='story-nearby'),
    path('friends/', FriendStoriesView.as_view(), name='story-friends'),
    path('mine/', MyStoriesView.as_view(), name='story-mine'),
    path('starred/', StarredStoriesView.as_view(), name='story-starred'),
    path('<int:pk>/react/', ReactToStoryView.as_view(), name='story-react'),
    path('<int:pk>/star/', StarStoryView.as_view(), name='story-star'),
    path('<int:pk>/', DeleteStoryView.as_view(), name='story-delete'),
]
