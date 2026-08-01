from django.urls import path
from .views import BulkSelectInterestsView, InterestListView, SelectInterestView

urlpatterns = [
    path('', InterestListView.as_view(), name='interest-list'),
    path('select/', BulkSelectInterestsView.as_view(), name='interest-bulk-select'),
    path('<int:pk>/select/', SelectInterestView.as_view(), name='interest-select'),
]
