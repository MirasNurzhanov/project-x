from django.urls import path

from .attestation_views import AttestationChallengeView, RegisterAttestationKeyView
from .moderation import (
    ApproveProofView,
    ModerationProofDetailView,
    ModerationProofListView,
    RejectProofView,
    RevokeProofView,
)
from .views import LeaderboardView, QuestListView, SubmitProofView

urlpatterns = [
    path('', QuestListView.as_view(), name='quest-list'),
    path('leaderboard/', LeaderboardView.as_view(), name='leaderboard'),
    path('attestation/challenge/', AttestationChallengeView.as_view(), name='attestation-challenge'),
    path('attestation/register/', RegisterAttestationKeyView.as_view(), name='attestation-register'),
    path('<int:pk>/submit-proof/', SubmitProofView.as_view(), name='quest-submit-proof'),
    # Moderation (staff / Moderator group only)
    path('moderation/proofs/', ModerationProofListView.as_view(), name='moderation-proof-list'),
    path('moderation/proofs/<int:pk>/', ModerationProofDetailView.as_view(), name='moderation-proof-detail'),
    path('moderation/proofs/<int:pk>/approve/', ApproveProofView.as_view(), name='moderation-proof-approve'),
    path('moderation/proofs/<int:pk>/reject/', RejectProofView.as_view(), name='moderation-proof-reject'),
    path('moderation/proofs/<int:pk>/revoke/', RevokeProofView.as_view(), name='moderation-proof-revoke'),
]
