from django.urls import path
from .views import AcknowledgeEncounterView, ClearEncountersView, EncounterListView

urlpatterns = [
    path('', EncounterListView.as_view(), name='encounter-list'),
    path('clear/', ClearEncountersView.as_view(), name='encounter-clear'),
    path('<int:pk>/acknowledge/', AcknowledgeEncounterView.as_view(), name='encounter-acknowledge'),
]
