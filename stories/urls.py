from django.urls import path
from .views import (
    CreateStoryView,
    DeleteStoryView,
    MyStoriesView,
    NearbyStoriesView,
    ReactToStoryView,
    SavedStoriesView,
    SaveStoryView,
)

urlpatterns = [
    path('', CreateStoryView.as_view(), name='story-create'),
    path('nearby/', NearbyStoriesView.as_view(), name='story-nearby'),
    path('mine/', MyStoriesView.as_view(), name='story-mine'),
    path('saved/', SavedStoriesView.as_view(), name='story-saved'),
    path('<int:pk>/react/', ReactToStoryView.as_view(), name='story-react'),
    path('<int:pk>/save/', SaveStoryView.as_view(), name='story-save'),
    path('<int:pk>/', DeleteStoryView.as_view(), name='story-delete'),
]
