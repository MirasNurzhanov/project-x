from django.urls import path
from .views import EncounterListView, AcknowledgeEncounterView

urlpatterns = [
    path('', EncounterListView.as_view(), name='encounter-list'),
    path('<int:pk>/acknowledge/', AcknowledgeEncounterView.as_view(), name='encounter-acknowledge'),
]
