"""Moderation API for quest photo proofs.

Separate from the user-facing quest endpoints and gated by a real permission
check on every request: Django staff or members of the `Moderator` group
(created by migration 0010). Approval is the only thing that moves points,
inside a transaction, exactly once per proof.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .ledger import award_for_proof, revoke_for_proof
from .models import Quest, QuestProof, UserQuest

MODERATOR_GROUP = 'Moderator'

PAGE_MAX = 100


class IsModerator(permissions.BasePermission):
    """Staff, or member of the Moderator group. Checked server-side on every
    moderation endpoint — the API is gated by role, not obscurity."""

    message = 'Moderator access required.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.is_staff or user.groups.filter(name=MODERATOR_GROUP).exists()


def _page_params(request, default_limit=25):
    try:
        limit = min(max(int(request.query_params.get('limit', default_limit)), 1), PAGE_MAX)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _serialize_proof(proof, detail=False):
    data = {
        'id': proof.pk,
        'status': proof.status,
        'submitted_at': proof.submitted_at.isoformat(),
        'window_start': proof.window_start.isoformat(),
        'user': {
            'id': proof.user_id,
            'username': proof.user.username,
        },
        'quest': {
            'id': proof.quest_id,
            'title': proof.quest.title,
            'quest_type': proof.quest.quest_type,
            'reward_points': proof.quest.reward_points,
        },
        'exif_capture_time': proof.exif_capture_time.isoformat() if proof.exif_capture_time else None,
        'exif_missing': proof.exif_capture_time is None,
        'attestation_status': proof.attestation_status,
        'attestation_platform': proof.attestation_platform,
        # Signed URL: unguessable without the API secret, but NOT time-limited
        # unless CLOUDINARY auth_token is configured. Treat it as a secret.
        'image_url': proof.image.url if proof.image else None,
    }
    if detail:
        data.update({
            'exif_latitude': str(proof.exif_latitude) if proof.exif_latitude is not None else None,
            'exif_longitude': str(proof.exif_longitude) if proof.exif_longitude is not None else None,
            'exif_data': proof.exif_data,
            'attestation_detail': proof.attestation_detail,
            'quest_description': proof.quest.description,
            'reviewer': proof.reviewer.username if proof.reviewer else None,
            'reviewed_at': proof.reviewed_at.isoformat() if proof.reviewed_at else None,
            'rejection_reason': proof.rejection_reason or None,
        })
    return data


class ModerationProofListView(APIView):
    permission_classes = [IsModerator]

    def get(self, request):
        qs = QuestProof.objects.select_related('user', 'quest').order_by('submitted_at')

        status_filter = request.query_params.get('status', QuestProof.Status.PENDING)
        if status_filter != 'all':
            if status_filter not in QuestProof.Status.values:
                return Response({'detail': 'Invalid status filter.'}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(status=status_filter)

        quest_type = request.query_params.get('quest_type')
        if quest_type:
            if quest_type not in Quest.QuestType.values:
                return Response({'detail': 'Invalid quest_type filter.'}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(quest__quest_type=quest_type)

        user_id = request.query_params.get('user')
        if user_id:
            qs = qs.filter(user_id=user_id)

        limit, offset = _page_params(request)
        count = qs.count()
        page = qs[offset:offset + limit]
        return Response({
            'count': count,
            'results': [_serialize_proof(p) for p in page],
        })


class ModerationProofDetailView(APIView):
    permission_classes = [IsModerator]

    def get(self, request, pk):
        try:
            proof = QuestProof.objects.select_related('user', 'quest', 'reviewer').get(pk=pk)
        except QuestProof.DoesNotExist:
            return Response({'detail': 'Proof not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_proof(proof, detail=True))


class ApproveProofView(APIView):
    permission_classes = [IsModerator]

    def post(self, request, pk):
        with transaction.atomic():
            try:
                proof = QuestProof.objects.select_for_update().select_related('quest').get(pk=pk)
            except QuestProof.DoesNotExist:
                return Response({'detail': 'Proof not found.'}, status=status.HTTP_404_NOT_FOUND)

            # Exactly-once: the row is locked, and an already-approved proof
            # can never award again.
            if proof.status == QuestProof.Status.APPROVED:
                return Response({'detail': 'Proof is already approved.'}, status=status.HTTP_409_CONFLICT)

            proof.status = QuestProof.Status.APPROVED
            proof.reviewer = request.user
            proof.reviewed_at = timezone.now()
            proof.rejection_reason = ''
            proof.save(update_fields=['status', 'reviewer', 'reviewed_at', 'rejection_reason'])

            user_quest, _ = UserQuest.objects.get_or_create(user=proof.user, quest=proof.quest)
            user_quest.completed = True
            user_quest.completed_at = proof.reviewed_at
            # The completion belongs to the window the photo was for, so the
            # quest resets on schedule even when review lags the submission.
            user_quest.reset_date = proof.window_start
            user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])

            award_for_proof(proof)

        return Response({
            'detail': 'Proof approved — points awarded.',
            'proof': _serialize_proof(QuestProof.objects.select_related('user', 'quest', 'reviewer').get(pk=pk), detail=True),
        })


class RejectProofView(APIView):
    permission_classes = [IsModerator]

    def post(self, request, pk):
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'detail': 'reason is required.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            try:
                proof = QuestProof.objects.select_for_update().get(pk=pk)
            except QuestProof.DoesNotExist:
                return Response({'detail': 'Proof not found.'}, status=status.HTTP_404_NOT_FOUND)

            if proof.status == QuestProof.Status.APPROVED:
                # Un-approving means taking points back — that is the revoke
                # flow (ledger-backed), not a plain rejection.
                return Response(
                    {'detail': 'Proof is already approved; use revoke instead.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if proof.status == QuestProof.Status.REJECTED:
                return Response({'detail': 'Proof is already rejected.'}, status=status.HTTP_409_CONFLICT)

            proof.status = QuestProof.Status.REJECTED
            proof.reviewer = request.user
            proof.reviewed_at = timezone.now()
            proof.rejection_reason = reason
            proof.save(update_fields=['status', 'reviewer', 'reviewed_at', 'rejection_reason'])

        return Response({'detail': 'Proof rejected — no points awarded.'})


class RevokeProofView(APIView):
    """Undo an approval: the proof becomes rejected and the points it earned
    are clawed back with a compensating ledger entry — including out of the
    weekly leaderboard, since the reversal lands in the same period."""
    permission_classes = [IsModerator]

    def post(self, request, pk):
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'detail': 'reason is required.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            try:
                proof = QuestProof.objects.select_for_update().select_related('quest').get(pk=pk)
            except QuestProof.DoesNotExist:
                return Response({'detail': 'Proof not found.'}, status=status.HTTP_404_NOT_FOUND)

            if proof.status != QuestProof.Status.APPROVED:
                return Response(
                    {'detail': 'Only an approved proof can be revoked.'},
                    status=status.HTTP_409_CONFLICT,
                )

            proof.status = QuestProof.Status.REJECTED
            proof.reviewer = request.user
            proof.reviewed_at = timezone.now()
            proof.rejection_reason = reason
            proof.save(update_fields=['status', 'reviewer', 'reviewed_at', 'rejection_reason'])

            # The quest is no longer completed for that window.
            UserQuest.objects.filter(user=proof.user, quest=proof.quest).update(
                completed=False, completed_at=None, reset_date=None,
            )

            new_total = revoke_for_proof(proof)

        return Response({
            'detail': 'Approval revoked — points removed.',
            'total_points': new_total,
        })
