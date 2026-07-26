from rest_framework import serializers
from .models import Quest, UserAuraPoints


class QuestSerializer(serializers.ModelSerializer):
    completed = serializers.SerializerMethodField()
    completed_at = serializers.SerializerMethodField()
    proof_status = serializers.SerializerMethodField()
    proof_rejection_reason = serializers.SerializerMethodField()

    class Meta:
        model = Quest
        fields = [
            'id', 'title', 'description', 'reward_points',
            'quest_type', 'condition_type', 'interest_id', 'completed', 'completed_at',
            'proof_status', 'proof_rejection_reason',
        ]

    def get_completed(self, obj):
        uq = self.context.get('user_quests', {}).get(obj.pk)
        return uq.completed if uq else False

    def get_completed_at(self, obj):
        uq = self.context.get('user_quests', {}).get(obj.pk)
        return uq.completed_at if uq else None

    def _current_proof(self, obj):
        """The user's latest proof for this quest, if it belongs to the
        quest's current window (older proofs are history, not state)."""
        proof = self.context.get('latest_proofs', {}).get(obj.pk)
        if proof is None:
            return None
        from .proof import quest_window
        from django.utils import timezone
        window_start, _ = quest_window(obj)
        if proof.window_start < timezone.localdate(window_start):
            return None
        return proof

    def get_proof_status(self, obj):
        proof = self._current_proof(obj)
        return proof.status if proof else None

    def get_proof_rejection_reason(self, obj):
        proof = self._current_proof(obj)
        if proof and proof.status == proof.Status.REJECTED:
            return proof.rejection_reason or None
        return None


class UserAuraPointsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAuraPoints
        fields = ['total_points']


# Leaderboard rows are built straight from the ledger aggregate in
# quests.views.LeaderboardView — there is no model instance to serialize.
