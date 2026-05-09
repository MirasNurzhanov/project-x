from django.urls import path
from .views import CompleteQuestView, QuestListView

urlpatterns = [
    path('', QuestListView.as_view(), name='quest-list'),
    path('<int:pk>/complete/', CompleteQuestView.as_view(), name='quest-complete'),
]
