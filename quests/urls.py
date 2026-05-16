from django.urls import path
from .views import CompleteQuestView, QuestListView, VerifyMeetupView

urlpatterns = [
    path('', QuestListView.as_view(), name='quest-list'),
    path('<int:pk>/complete/', CompleteQuestView.as_view(), name='quest-complete'),
    path('<int:pk>/verify-meetup/', VerifyMeetupView.as_view(), name='quest-verify-meetup'),
]
