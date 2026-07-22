from django.urls import path
from .views import BulkSelectInterestsView, InterestListView, OtherInterestView, SelectInterestView

urlpatterns = [
    path('', InterestListView.as_view(), name='interest-list'),
    path('other/', OtherInterestView.as_view(), name='interest-other'),
    path('select/', BulkSelectInterestsView.as_view(), name='interest-bulk-select'),
    path('<int:pk>/select/', SelectInterestView.as_view(), name='interest-select'),
]
