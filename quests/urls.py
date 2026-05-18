from django.urls import path
from .views import CompleteQuestView, LeaderboardView, QuestListView, VerifyMeetupView

urlpatterns = [
    path('', QuestListView.as_view(), name='quest-list'),
    path('leaderboard/', LeaderboardView.as_view(), name='leaderboard'),
    path('<int:pk>/complete/', CompleteQuestView.as_view(), name='quest-complete'),
    path('<int:pk>/verify-meetup/', VerifyMeetupView.as_view(), name='quest-verify-meetup'),
]
