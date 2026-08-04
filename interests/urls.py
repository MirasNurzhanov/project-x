from django.urls import path
from .views import (
    BulkSelectInterestsView,
    InterestCategoryListView,
    InterestListView,
    SelectInterestView,
)

urlpatterns = [
    path('', InterestListView.as_view(), name='interest-list'),
    # Before the <int:pk> route so the word is never read as an id.
    path('categories/', InterestCategoryListView.as_view(), name='interest-categories'),
    path('select/', BulkSelectInterestsView.as_view(), name='interest-bulk-select'),
    path('<int:pk>/select/', SelectInterestView.as_view(), name='interest-select'),
]
