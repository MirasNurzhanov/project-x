import random
from datetime import timedelta

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from users import visibility
from users.models import User
from .attestation import attest_submission
from .content import DAILY_OFFER_COUNT
from .ledger import current_week_start, weekly_rank_for, weekly_standings
from .models import Quest, QuestProof, UserAuraPoints, UserQuest
from .proof import MAX_IMAGE_BYTES, capture_time_ok, extract_exif, quest_window
from .serializers import QuestSerializer
from .utils import _is_reset_due


class LeaderboardView(APIView):
    """Weekly, global. Points come from the PointEvent ledger, which only
    receives leaderboard-eligible entries when a moderator approves a photo
    proof — pending and rejected proofs contribute nothing, and a revoked
    approval nets back out of the same week."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        week_start = current_week_start()

        # Anyone who opted out of public rankings, plus both directions of a
        # block, is dropped before the top-N cut so the board has no gaps.
        hidden_ids = visibility.blocked_ids_for(request.user) | set(
            User.objects.filter(show_on_leaderboard=False).values_list('pk', flat=True)
        )
        rows = weekly_standings(week_start, exclude_user_ids=hidden_ids)

        users = User.objects.filter(pk__in=[r['user_id'] for r in rows]).only(
            'id', 'username', 'profile_picture'
        )
        by_id = {u.pk: u for u in users}

        leaderboard = []
        for rank, row in enumerate(rows, start=1):
            user = by_id.get(row['user_id'])
            if user is None:
                continue
            leaderboard.append({
                'rank': rank,
                'username': user.username,
                'profile_picture': user.profile_picture.url if user.profile_picture else None,
                'total_points': row['total'],
            })

        my_rank, my_points = weekly_rank_for(request.user, week_start, hidden_ids)

        return Response({
            'period': 'weekly',
            'scope': 'global',
            'period_start': week_start.isoformat(),
            'period_end': (week_start + timedelta(days=6)).isoformat(),
            'my_rank': my_rank,
            'my_points': my_points,
            'leaderboard': leaderboard,
        })


class QuestListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _daily_offer(user):
        """Today's daily quests for this user: a stable pseudo-random sample
        of their interest pool, fixed for the whole day so the list does not
        reshuffle between refreshes.

        Anything they already submitted a proof for today stays in the list
        even if it was not drawn — otherwise a quest would vanish while its
        photo was still under review and the user would never see the verdict.
        """
        today = timezone.localdate()

        pool = list(
            Quest.objects
            .filter(quest_type=Quest.QuestType.DAILY, interest__in=user.interests.all())
            .order_by('pk')
        )
        if len(pool) > DAILY_OFFER_COUNT:
            rng = random.Random(f'{user.pk}:{today.isoformat()}')
            offered = rng.sample(pool, DAILY_OFFER_COUNT)
        else:
            offered = pool

        engaged = Quest.objects.filter(
            quest_type=Quest.QuestType.DAILY,
            pk__in=QuestProof.objects.filter(
                user=user, window_start=today,
            ).values('quest_id'),
        )

        by_id = {q.pk: q for q in offered}
        for quest in engaged:
            by_id.setdefault(quest.pk, quest)
        return sorted(by_id.values(), key=lambda q: q.pk)

    def get(self, request):
        user = request.user

        # Weeklies are interest-agnostic: everyone gets the same ten, so the
        # weekly leaderboard offers every player the same ceiling.
        weekly_quests = list(
            Quest.objects.filter(quest_type=Quest.QuestType.WEEKLY, interest__isnull=True)
        )

        # Dailies are drawn from the pool of quests for the user's interests —
        # a fixed number per day, rotating deterministically. Offering the
        # whole pool would both flood the screen and hand users with many
        # interests a far higher daily ceiling than everyone else.
        daily_quests = self._daily_offer(user)

        all_quests = daily_quests + weekly_quests
        user_quests = {
            uq.quest_id: uq
            for uq in UserQuest.objects.filter(user=user, quest__in=all_quests)
        }
        # Latest proof per quest so cards can show pending/rejected states.
        # Ordered ascending: the last write per quest_id wins.
        latest_proofs = {}
        for proof in QuestProof.objects.filter(user=user, quest__in=all_quests).order_by('submitted_at'):
            latest_proofs[proof.quest_id] = proof
        aura = UserAuraPoints.objects.filter(user=user).first()
        serializer = QuestSerializer(
            all_quests, many=True,
            context={'user_quests': user_quests, 'latest_proofs': latest_proofs},
        )
        return Response({
            'total_points': aura.total_points if aura else 0,
            'quests': serializer.data,
        })


class SubmitProofView(APIView):
    """POST a camera photo as proof of quest completion.

    The submission only creates a PENDING QuestProof — no points move until a
    moderator approves it. The server records its own received-at time and
    extracts EXIF from the uploaded bytes; client metadata is never trusted.
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        try:
            quest = Quest.objects.get(pk=pk)
        except Quest.DoesNotExist:
            return Response({'detail': 'Quest not found.'}, status=status.HTTP_404_NOT_FOUND)

        image = request.FILES.get('image')
        if image is None:
            return Response({'detail': 'image is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if image.size > MAX_IMAGE_BYTES:
            return Response({'detail': 'Image is too large (max 15 MB).'}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        window_start, _window_end = quest_window(quest, now)

        user_quest = UserQuest.objects.filter(user=request.user, quest=quest).first()
        if user_quest and user_quest.completed and not _is_reset_due(user_quest):
            return Response(
                {'detail': 'Quest already completed for this period.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if QuestProof.objects.filter(
            user=request.user, quest=quest, status=QuestProof.Status.PENDING,
        ).exists():
            return Response(
                {'detail': 'You already have a proof under review for this quest.'},
                status=status.HTTP_409_CONFLICT,
            )

        exif, error = extract_exif(image)
        if error:
            return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)

        if not capture_time_ok(exif['capture_time'], window_start, now):
            return Response(
                {'detail': 'This photo was taken outside the quest window. '
                           'Take a fresh photo with your camera to complete the quest.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # EXIF cannot tell a camera shot from a gallery pick. Device
        # attestation can: it proves the request came from a genuine,
        # unmodified build, and a genuine build has no gallery path here.
        attestation = attest_submission(request.user, request.data)
        if attestation.blocks_submission:
            return Response(
                {'detail': 'This device could not be verified, so the photo was not accepted. '
                           'Make sure you are using the official app on an unmodified device.',
                 'attestation_status': attestation.status},
                status=status.HTTP_403_FORBIDDEN,
            )

        proof = QuestProof.objects.create(
            user=request.user,
            quest=quest,
            image=image,
            window_start=timezone.localdate(window_start),
            exif_capture_time=exif['capture_time'],
            exif_latitude=exif['latitude'],
            exif_longitude=exif['longitude'],
            exif_data=exif['raw'],
            attestation_status=attestation.status,
            attestation_platform=attestation.platform,
            attestation_detail=attestation.detail,
        )

        return Response({
            'detail': 'Proof submitted — pending review.',
            'proof_id': proof.pk,
            'status': proof.status,
            'submitted_at': proof.submitted_at.isoformat(),
            'attestation_status': proof.attestation_status,
        }, status=status.HTTP_201_CREATED)
