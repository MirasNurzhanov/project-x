from django.urls import path
from .views import InterestListView, SelectInterestView

urlpatterns = [
    path('', InterestListView.as_view(), name='interest-list'),
    path('<int:pk>/select/', SelectInterestView.as_view(), name='interest-select'),
]
