from django.urls import path
from .views import CreateStoryView, NearbyStoriesView, ReactToStoryView, DeleteStoryView

urlpatterns = [
    path('', CreateStoryView.as_view(), name='story-create'),
    path('nearby/', NearbyStoriesView.as_view(), name='story-nearby'),
    path('<int:pk>/react/', ReactToStoryView.as_view(), name='story-react'),
    path('<int:pk>/', DeleteStoryView.as_view(), name='story-delete'),
]
