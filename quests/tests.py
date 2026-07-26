"""Photo-proof quest system tests.

Run with:  python manage.py test quests --settings=core.test_settings
"""
import io
from datetime import timedelta

from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from users.models import User
from .models import Quest, QuestProof


def make_user(name):
    return User.objects.create_user(email=f'{name}@test.dev', username=name, password='pw')


def jpeg_bytes(capture_time=None):
    """A tiny JPEG; when capture_time (aware or naive dt) is given, embed it
    as EXIF DateTimeOriginal — like a real camera photo. Without it, the file
    has no EXIF, like a stripped/edited image."""
    img = Image.new('RGB', (32, 32), color=(120, 40, 200))
    buf = io.BytesIO()
    if capture_time is not None:
        exif = Image.Exif()
        exif[0x9003] = capture_time.strftime('%Y:%m:%d %H:%M:%S')
        img.save(buf, format='JPEG', exif=exif)
    else:
        img.save(buf, format='JPEG')
    buf.seek(0)
    buf.name = 'proof.jpg'
    return buf


class ProofSubmissionTests(APITestCase):
    def setUp(self):
        self.user = make_user('runner')
        self.quest = Quest.objects.create(
            title='Daily quest', description='d', reward_points=50,
            quest_type=Quest.QuestType.DAILY,
            condition_type=Quest.ConditionType.SEND_HI,
        )
        self.client.force_authenticate(user=self.user)

    def submit(self, photo):
        return self.client.post(
            f'/api/quests/{self.quest.pk}/submit-proof/',
            {'image': photo}, format='multipart',
        )

    def test_fresh_photo_creates_pending_proof(self):
        res = self.submit(jpeg_bytes(timezone.now()))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        proof = QuestProof.objects.get()
        self.assertEqual(proof.status, QuestProof.Status.PENDING)
        self.assertIsNotNone(proof.exif_capture_time)
        # Server recorded its own clock, not the client's.
        self.assertLess((timezone.now() - proof.submitted_at).total_seconds(), 10)

    def test_stale_exif_rejected(self):
        # A gallery photo from three days ago — far outside today's window.
        res = self.submit(jpeg_bytes(timezone.now() - timedelta(days=3)))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('outside the quest window', res.data['detail'])
        self.assertEqual(QuestProof.objects.count(), 0)

    def test_future_exif_rejected(self):
        res = self.submit(jpeg_bytes(timezone.now() + timedelta(days=2)))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(QuestProof.objects.count(), 0)

    def test_missing_exif_accepted_for_moderator(self):
        # Some devices strip EXIF; the automatic check passes it through and
        # the moderator sees the absence.
        res = self.submit(jpeg_bytes())
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNone(QuestProof.objects.get().exif_capture_time)

    def test_non_image_rejected(self):
        buf = io.BytesIO(b'this is not an image')
        buf.name = 'proof.jpg'
        res = self.submit(buf)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_second_pending_proof_blocked(self):
        self.assertEqual(self.submit(jpeg_bytes(timezone.now())).status_code, 201)
        res = self.submit(jpeg_bytes(timezone.now()))
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(QuestProof.objects.count(), 1)

    def test_retry_allowed_after_rejection(self):
        self.assertEqual(self.submit(jpeg_bytes(timezone.now())).status_code, 201)
        QuestProof.objects.update(status=QuestProof.Status.REJECTED)
        res = self.submit(jpeg_bytes(timezone.now()))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_weekly_window_accepts_older_photo_within_week(self):
        weekly = Quest.objects.create(
            title='Weekly quest', description='w', reward_points=150,
            quest_type=Quest.QuestType.WEEKLY,
            condition_type=Quest.ConditionType.SEND_HI,
        )
        # Weekday number = days since Monday; a photo from the window start
        # is valid for the weekly quest even when that was days ago.
        days_into_week = timezone.localdate().weekday()
        photo = jpeg_bytes(timezone.now() - timedelta(days=days_into_week, hours=2))
        res = self.client.post(
            f'/api/quests/{weekly.pk}/submit-proof/',
            {'image': photo}, format='multipart',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

    def test_self_claim_endpoint_removed(self):
        res = self.client.post(f'/api/quests/{self.quest.pk}/complete/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_submission_awards_no_points(self):
        from .models import UserAuraPoints
        self.submit(jpeg_bytes(timezone.now()))
        aura = UserAuraPoints.objects.filter(user=self.user).first()
        self.assertTrue(aura is None or aura.total_points == 0)


class ModerationTests(APITestCase):
    def setUp(self):
        from django.contrib.auth.models import Group

        self.player = make_user('player')
        self.rando = make_user('rando')
        self.staff = make_user('staff')
        self.staff.is_staff = True
        self.staff.save(update_fields=['is_staff'])
        self.mod = make_user('mod')
        group, _ = Group.objects.get_or_create(name='Moderator')
        self.mod.groups.add(group)

        self.quest = Quest.objects.create(
            title='Daily quest', description='d', reward_points=50,
            quest_type=Quest.QuestType.DAILY,
            condition_type=Quest.ConditionType.SEND_HI,
        )

    def submit_proof(self, user=None, quest=None):
        self.client.force_authenticate(user=user or self.player)
        res = self.client.post(
            f'/api/quests/{(quest or self.quest).pk}/submit-proof/',
            {'image': jpeg_bytes(timezone.now())}, format='multipart',
        )
        assert res.status_code == 201, res.data
        return res.data['proof_id']

    def points_of(self, user):
        from .models import UserAuraPoints
        aura = UserAuraPoints.objects.filter(user=user).first()
        return aura.total_points if aura else 0

    def test_non_moderator_blocked_everywhere(self):
        proof_id = self.submit_proof()
        self.client.force_authenticate(user=self.rando)
        for method, url, body in [
            ('get', '/api/quests/moderation/proofs/', None),
            ('get', f'/api/quests/moderation/proofs/{proof_id}/', None),
            ('post', f'/api/quests/moderation/proofs/{proof_id}/approve/', None),
            ('post', f'/api/quests/moderation/proofs/{proof_id}/reject/', {'reason': 'x'}),
        ]:
            res = getattr(self.client, method)(url, body)
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, url)
        # And blocked users can't sneak points: nothing was approved.
        self.assertEqual(self.points_of(self.player), 0)

    def test_staff_and_group_moderators_allowed(self):
        self.submit_proof()
        for reviewer in (self.staff, self.mod):
            self.client.force_authenticate(user=reviewer)
            res = self.client.get('/api/quests/moderation/proofs/')
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            self.assertEqual(res.data['count'], 1)

    def test_approve_awards_points_exactly_once(self):
        proof_id = self.submit_proof()
        self.client.force_authenticate(user=self.mod)

        res = self.client.post(f'/api/quests/moderation/proofs/{proof_id}/approve/')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.points_of(self.player), 50)

        # Double-approve is refused and does not double-award.
        res = self.client.post(f'/api/quests/moderation/proofs/{proof_id}/approve/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.points_of(self.player), 50)

        proof = QuestProof.objects.get(pk=proof_id)
        self.assertEqual(proof.reviewer, self.mod)
        self.assertIsNotNone(proof.reviewed_at)

    def test_approval_marks_quest_completed_for_user(self):
        proof_id = self.submit_proof()
        self.client.force_authenticate(user=self.mod)
        self.client.post(f'/api/quests/moderation/proofs/{proof_id}/approve/')

        self.client.force_authenticate(user=self.player)
        res = self.client.get('/api/quests/')
        quest_data = next(q for q in res.data['quests'] if q['id'] == self.quest.pk)
        self.assertTrue(quest_data['completed'])
        self.assertEqual(quest_data['proof_status'], 'approved')
        self.assertEqual(res.data['total_points'], 50)

    def test_reject_requires_reason_and_awards_nothing(self):
        proof_id = self.submit_proof()
        self.client.force_authenticate(user=self.mod)

        res = self.client.post(f'/api/quests/moderation/proofs/{proof_id}/reject/', {})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        res = self.client.post(
            f'/api/quests/moderation/proofs/{proof_id}/reject/',
            {'reason': 'Photo does not show the activity.'},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(self.points_of(self.player), 0)

        proof = QuestProof.objects.get(pk=proof_id)
        self.assertEqual(proof.status, QuestProof.Status.REJECTED)
        self.assertEqual(proof.rejection_reason, 'Photo does not show the activity.')

        # The player sees the rejection on their quest card.
        self.client.force_authenticate(user=self.player)
        res = self.client.get('/api/quests/')
        quest_data = next(q for q in res.data['quests'] if q['id'] == self.quest.pk)
        self.assertFalse(quest_data['completed'])
        self.assertEqual(quest_data['proof_status'], 'rejected')
        self.assertEqual(quest_data['proof_rejection_reason'], 'Photo does not show the activity.')

    def test_reject_approved_proof_refused(self):
        proof_id = self.submit_proof()
        self.client.force_authenticate(user=self.mod)
        self.client.post(f'/api/quests/moderation/proofs/{proof_id}/approve/')
        res = self.client.post(
            f'/api/quests/moderation/proofs/{proof_id}/reject/', {'reason': 'oops'},
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.points_of(self.player), 50)

    def test_pending_list_oldest_first_filtered_paginated(self):
        other_quest = Quest.objects.create(
            title='Weekly quest', description='w', reward_points=150,
            quest_type=Quest.QuestType.WEEKLY,
            condition_type=Quest.ConditionType.SEND_MESSAGE,
        )
        first = self.submit_proof(self.player, self.quest)
        second = self.submit_proof(self.rando, self.quest)
        third = self.submit_proof(self.player, other_quest)

        self.client.force_authenticate(user=self.mod)

        res = self.client.get('/api/quests/moderation/proofs/')
        self.assertEqual(res.data['count'], 3)
        self.assertEqual([p['id'] for p in res.data['results']], [first, second, third])

        res = self.client.get('/api/quests/moderation/proofs/', {'limit': 2})
        self.assertEqual(res.data['count'], 3)
        self.assertEqual(len(res.data['results']), 2)

        res = self.client.get('/api/quests/moderation/proofs/', {'limit': 2, 'offset': 2})
        self.assertEqual([p['id'] for p in res.data['results']], [third])

        res = self.client.get('/api/quests/moderation/proofs/', {'quest_type': 'weekly'})
        self.assertEqual([p['id'] for p in res.data['results']], [third])

        # Approve one; default (pending) list shrinks, status=all still shows it.
        self.client.post(f'/api/quests/moderation/proofs/{first}/approve/')
        res = self.client.get('/api/quests/moderation/proofs/')
        self.assertEqual(res.data['count'], 2)
        res = self.client.get('/api/quests/moderation/proofs/', {'status': 'all'})
        self.assertEqual(res.data['count'], 3)


class LedgerAndLeaderboardTests(APITestCase):
    def setUp(self):
        from django.contrib.auth.models import Group

        self.alice = make_user('alice')
        self.bob = make_user('bob')
        self.mod = make_user('mod')
        group, _ = Group.objects.get_or_create(name='Moderator')
        self.mod.groups.add(group)

        self.quest = Quest.objects.create(
            title='Daily quest', description='d', reward_points=50,
            quest_type=Quest.QuestType.DAILY,
            condition_type=Quest.ConditionType.SEND_HI,
        )
        self.big_quest = Quest.objects.create(
            title='Weekly quest', description='w', reward_points=150,
            quest_type=Quest.QuestType.WEEKLY,
            condition_type=Quest.ConditionType.SEND_MESSAGE,
        )

    def submit(self, user, quest):
        self.client.force_authenticate(user=user)
        res = self.client.post(
            f'/api/quests/{quest.pk}/submit-proof/',
            {'image': jpeg_bytes(timezone.now())}, format='multipart',
        )
        assert res.status_code == 201, res.data
        return res.data['proof_id']

    def approve(self, proof_id):
        self.client.force_authenticate(user=self.mod)
        res = self.client.post(f'/api/quests/moderation/proofs/{proof_id}/approve/')
        assert res.status_code == 200, res.data

    def reject(self, proof_id, reason='no'):
        self.client.force_authenticate(user=self.mod)
        res = self.client.post(
            f'/api/quests/moderation/proofs/{proof_id}/reject/', {'reason': reason},
        )
        assert res.status_code == 200, res.data

    def leaderboard(self, as_user):
        self.client.force_authenticate(user=as_user)
        res = self.client.get('/api/quests/leaderboard/')
        assert res.status_code == 200, res.data
        return res.data

    def test_leaderboard_counts_approved_proofs_only(self):
        approved = self.submit(self.alice, self.quest)
        self.approve(approved)

        # Bob has one pending and one rejected proof — worth nothing.
        pending = self.submit(self.bob, self.quest)  # noqa: F841 (left pending)
        rejected = self.submit(self.bob, self.big_quest)
        self.reject(rejected)

        data = self.leaderboard(self.alice)
        self.assertEqual(data['period'], 'weekly')
        self.assertEqual(data['scope'], 'global')
        self.assertEqual([e['username'] for e in data['leaderboard']], ['alice'])
        self.assertEqual(data['leaderboard'][0]['total_points'], 50)
        self.assertEqual(data['my_rank'], 1)

        # Bob is not ranked at all.
        bob_view = self.leaderboard(self.bob)
        self.assertIsNone(bob_view['my_rank'])
        self.assertEqual(bob_view['my_points'], 0)

    def test_leaderboard_totals_match_sum_of_approved_rewards(self):
        self.approve(self.submit(self.alice, self.quest))       # 50
        self.approve(self.submit(self.alice, self.big_quest))   # 150
        self.approve(self.submit(self.bob, self.big_quest))     # 150

        data = self.leaderboard(self.alice)
        standings = {e['username']: e['total_points'] for e in data['leaderboard']}
        self.assertEqual(standings, {'alice': 200, 'bob': 150})
        self.assertEqual([e['rank'] for e in data['leaderboard']], [1, 2])

    def test_ties_break_deterministically_by_who_scored_first(self):
        self.approve(self.submit(self.bob, self.quest))    # bob scores first
        self.approve(self.submit(self.alice, self.quest))  # same 50 points

        data = self.leaderboard(self.alice)
        self.assertEqual([e['username'] for e in data['leaderboard']], ['bob', 'alice'])
        self.assertEqual([e['rank'] for e in data['leaderboard']], [1, 2])
        # Stable across repeated calls.
        again = self.leaderboard(self.alice)
        self.assertEqual(
            [e['username'] for e in again['leaderboard']],
            [e['username'] for e in data['leaderboard']],
        )

    def test_revoke_removes_points_from_total_and_leaderboard(self):
        from .models import PointEvent, UserAuraPoints

        proof_id = self.submit(self.alice, self.quest)
        self.approve(proof_id)
        self.assertEqual(UserAuraPoints.objects.get(user=self.alice).total_points, 50)

        self.client.force_authenticate(user=self.mod)
        res = self.client.post(
            f'/api/quests/moderation/proofs/{proof_id}/revoke/',
            {'reason': 'Photo was staged.'},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        self.assertEqual(UserAuraPoints.objects.get(user=self.alice).total_points, 0)
        self.assertEqual(self.leaderboard(self.alice)['leaderboard'], [])

        # Auditable: award and clawback both survive as ledger rows.
        events = list(PointEvent.objects.filter(user=self.alice).values_list('points', 'reason'))
        self.assertEqual(events, [(50, 'quest_approved'), (-50, 'quest_revoked')])

        # The quest is completable again.
        proof = QuestProof.objects.get(pk=proof_id)
        self.assertEqual(proof.status, QuestProof.Status.REJECTED)
        self.assertFalse(
            __import__('quests.models', fromlist=['UserQuest']).UserQuest.objects.get(
                user=self.alice, quest=self.quest,
            ).completed
        )

    def test_revoke_is_not_double_charged(self):
        from .models import UserAuraPoints

        proof_id = self.submit(self.alice, self.quest)
        self.approve(proof_id)
        self.client.force_authenticate(user=self.mod)
        self.client.post(f'/api/quests/moderation/proofs/{proof_id}/revoke/', {'reason': 'x'})

        # Second revoke is refused (no longer approved) — total stays at 0,
        # never negative.
        res = self.client.post(f'/api/quests/moderation/proofs/{proof_id}/revoke/', {'reason': 'x'})
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(UserAuraPoints.objects.get(user=self.alice).total_points, 0)

    def test_unverified_awards_never_reach_weekly_leaderboard(self):
        from .models import PointEvent, UserAuraPoints
        from .utils import check_and_complete_quest

        # Event-driven completion (e.g. sending a Hi) still credits the
        # all-time total but must not appear in the verified weekly board.
        # Migrations seed their own send_hi quest, so assert against whichever
        # row the helper actually picks.
        expected = (
            Quest.objects
            .filter(condition_type=Quest.ConditionType.SEND_HI, interest__isnull=True)
            .order_by('pk').first()
        )
        check_and_complete_quest(self.alice, Quest.ConditionType.SEND_HI)

        self.assertEqual(
            UserAuraPoints.objects.get(user=self.alice).total_points,
            expected.reward_points,
        )
        self.assertTrue(
            PointEvent.objects.filter(user=self.alice, period_start__isnull=True).exists()
        )
        self.assertEqual(self.leaderboard(self.alice)['leaderboard'], [])

    def test_legacy_balance_backfill_preserves_totals_without_polluting_week(self):
        """The 0012 migration runs against live Railway data, so exercise its
        logic directly: existing balances must survive as all-time-only rows."""
        import importlib

        from django.apps import apps as real_apps

        from .models import PointEvent, UserAuraPoints

        backfill = importlib.import_module(
            'quests.migrations.0012_backfill_point_ledger'
        ).backfill

        UserAuraPoints.objects.update_or_create(user=self.alice, defaults={'total_points': 320})
        UserAuraPoints.objects.update_or_create(user=self.bob, defaults={'total_points': 0})

        backfill(real_apps, None)

        alice_events = PointEvent.objects.filter(user=self.alice, reason='legacy_balance')
        self.assertEqual(alice_events.count(), 1)
        self.assertEqual(alice_events.first().points, 320)
        self.assertIsNone(alice_events.first().period_start)
        # Zero balances create no noise.
        self.assertFalse(PointEvent.objects.filter(user=self.bob).exists())
        # Legacy points stay out of the verified weekly board.
        self.assertEqual(self.leaderboard(self.alice)['leaderboard'], [])

        # Re-running the migration must not double-credit anyone.
        backfill(real_apps, None)
        self.assertEqual(
            PointEvent.objects.filter(user=self.alice, reason='legacy_balance').count(), 1
        )

    def test_profile_exposes_ledger_total(self):
        self.approve(self.submit(self.alice, self.quest))
        self.client.force_authenticate(user=self.alice)
        res = self.client.get('/api/users/profile/')
        self.assertEqual(res.data['aura_points'], 50)

        # A user with no points at all still serializes cleanly.
        self.client.force_authenticate(user=self.bob)
        res = self.client.get('/api/users/profile/')
        self.assertEqual(res.data['aura_points'], 0)


class QuestCatalogueTests(APITestCase):
    """The seeded catalogue: coverage, retirement of unverifiable quests, and
    the daily rotation that keeps everyone's daily ceiling equal."""

    def setUp(self):
        from interests.models import Interest
        self.interests = list(Interest.objects.all())
        self.user = make_user('seeker')

    def test_every_interest_has_twenty_dailies(self):
        from .content import DAILY_BY_CATEGORY

        self.assertGreater(len(self.interests), 0, 'interests should be seeded')
        for interest in self.interests:
            count = Quest.objects.filter(
                interest=interest, quest_type=Quest.QuestType.DAILY,
            ).count()
            self.assertEqual(count, 20, f'{interest.name} has {count} daily quests')

        # Every category ships exactly 20 authored templates.
        for name, templates in DAILY_BY_CATEGORY.items():
            self.assertEqual(len(templates), 20, f'category {name} has {len(templates)}')

    def test_ten_interest_agnostic_weeklies(self):
        weeklies = Quest.objects.filter(
            quest_type=Quest.QuestType.WEEKLY, interest__isnull=True,
        )
        self.assertEqual(weeklies.count(), 10)
        # None are tied to an interest, so every player can earn all of them.
        self.assertFalse(
            Quest.objects.filter(quest_type=Quest.QuestType.WEEKLY, interest__isnull=False).exists()
        )

    def test_weeklies_pay_more_than_dailies(self):
        max_daily = max(
            Quest.objects.filter(quest_type=Quest.QuestType.DAILY)
            .values_list('reward_points', flat=True)
        )
        min_weekly = min(
            Quest.objects.filter(quest_type=Quest.QuestType.WEEKLY)
            .values_list('reward_points', flat=True)
        )
        self.assertGreater(min_weekly, max_daily)

    def test_unverifiable_quests_retired(self):
        for condition in ['send_hi', 'send_message', 'update_location',
                          'make_friend', 'have_encounter', 'meet_friend_in_person']:
            self.assertFalse(
                Quest.objects.filter(condition_type=condition).exists(),
                f'{condition} quests should be retired',
            )
        # Everything left is photo-verified.
        self.assertFalse(
            Quest.objects.exclude(condition_type=Quest.ConditionType.PHOTO_PROOF).exists()
        )

    def test_every_quest_is_translatable_and_photo_shaped(self):
        for quest in Quest.objects.all():
            self.assertTrue(quest.template_key, f'{quest.title} has no template key')
            self.assertIsInstance(quest.template_params, dict)
            # No unrendered placeholders leaked into stored text.
            self.assertNotIn('{', quest.title)
            self.assertNotIn('{', quest.description)
            if quest.interest_id:
                self.assertEqual(quest.template_params.get('interest'), quest.interest.name)

    def test_daily_offer_is_capped_and_stable(self):
        from .content import DAILY_OFFER_COUNT

        # Give the user several interests: the pool is large, the offer is not.
        self.user.interests.set(self.interests[:4])
        self.client.force_authenticate(user=self.user)

        first = self.client.get('/api/quests/').data['quests']
        dailies = [q for q in first if q['quest_type'] == 'daily']
        weeklies = [q for q in first if q['quest_type'] == 'weekly']

        self.assertEqual(len(dailies), DAILY_OFFER_COUNT)
        self.assertEqual(len(weeklies), 10)

        # Stable across refreshes within the same day.
        second = self.client.get('/api/quests/').data['quests']
        self.assertEqual(
            [q['id'] for q in second if q['quest_type'] == 'daily'],
            [q['id'] for q in dailies],
        )

    def test_daily_ceiling_equal_regardless_of_interest_count(self):
        from .content import DAILY_OFFER_COUNT

        one_interest = make_user('minimal')
        one_interest.interests.set(self.interests[:1])
        many_interests = make_user('maximal')
        many_interests.interests.set(self.interests)

        for user in (one_interest, many_interests):
            self.client.force_authenticate(user=user)
            dailies = [
                q for q in self.client.get('/api/quests/').data['quests']
                if q['quest_type'] == 'daily'
            ]
            self.assertEqual(len(dailies), DAILY_OFFER_COUNT)

    def test_offer_draws_only_from_the_users_interests(self):
        chosen = self.interests[0]
        self.user.interests.set([chosen])
        self.client.force_authenticate(user=self.user)

        dailies = [
            q for q in self.client.get('/api/quests/').data['quests']
            if q['quest_type'] == 'daily'
        ]
        for quest in dailies:
            self.assertEqual(quest['interest_id'], chosen.id)

    def test_quest_under_review_stays_visible_even_if_not_drawn(self):
        """A quest whose photo is awaiting review must not disappear from the
        list, or the user would never see the moderator's verdict."""
        self.user.interests.set(self.interests[:4])
        self.client.force_authenticate(user=self.user)

        # Submit against a daily quest that today's rotation did not offer.
        offered_ids = {
            q['id'] for q in self.client.get('/api/quests/').data['quests']
            if q['quest_type'] == 'daily'
        }
        not_offered = (
            Quest.objects
            .filter(quest_type=Quest.QuestType.DAILY, interest__in=self.interests[:4])
            .exclude(pk__in=offered_ids)
            .first()
        )
        self.assertIsNotNone(not_offered)

        res = self.client.post(
            f'/api/quests/{not_offered.pk}/submit-proof/',
            {'image': jpeg_bytes(timezone.now())}, format='multipart',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

        quests = self.client.get('/api/quests/').data['quests']
        shown = next((q for q in quests if q['id'] == not_offered.pk), None)
        self.assertIsNotNone(shown, 'quest under review vanished from the list')
        self.assertEqual(shown['proof_status'], 'pending')

    def test_user_without_interests_still_gets_weeklies(self):
        self.client.force_authenticate(user=self.user)
        quests = self.client.get('/api/quests/').data['quests']
        self.assertEqual([q for q in quests if q['quest_type'] == 'daily'], [])
        self.assertEqual(len([q for q in quests if q['quest_type'] == 'weekly']), 10)

    def test_sync_is_idempotent_and_covers_new_interests(self):
        from interests.models import Interest

        from .sync import sync_quests

        before = Quest.objects.count()
        daily, weekly = sync_quests(Quest, Interest)
        self.assertEqual((daily, weekly), (0, 0))
        self.assertEqual(Quest.objects.count(), before)

        # An interest added later (e.g. a user "Other" entry) gets quests.
        new_interest = Interest.objects.create(name='Birdwatching', icon='🦅')
        daily, weekly = sync_quests(Quest, Interest)
        self.assertEqual(daily, 20)
        self.assertEqual(weekly, 0)
        self.assertEqual(
            Quest.objects.filter(interest=new_interest).count(), 20
        )

    def test_unmapped_interest_falls_back_sensibly(self):
        from .content import category_for

        # Known names map to their category; keyword matching catches names
        # added after the catalogue was written; anything else is generic.
        self.assertEqual(category_for('Guitar'), 'music')
        self.assertEqual(category_for('Rock Climbing'), 'sports')
        self.assertEqual(category_for('Sourdough Baking'), 'food')
        self.assertEqual(category_for('Zorbing'), 'generic')


class AttestationTests(APITestCase):
    """Device attestation: challenge lifecycle, enforcement modes, and a real
    Apple App Attest verification using pyattest's test factory."""

    APP_ID = '1234ABCDEF.com.arcanix.aura'

    def setUp(self):
        self.user = make_user('attester')
        self.quest = Quest.objects.create(
            title='Attest quest', description='d', reward_points=50,
            quest_type=Quest.QuestType.DAILY,
            condition_type=Quest.ConditionType.PHOTO_PROOF,
        )
        self.client.force_authenticate(user=self.user)

    # ── helpers ──────────────────────────────────────────────────────────────
    def get_challenge(self):
        res = self.client.post('/api/quests/attestation/challenge/')
        assert res.status_code == 201, res.data
        return res.data['nonce']

    def submit(self, **extra):
        payload = {'image': jpeg_bytes(timezone.now())}
        payload.update(extra)
        return self.client.post(
            f'/api/quests/{self.quest.pk}/submit-proof/', payload, format='multipart',
        )

    @staticmethod
    def apple_root_ca():
        import os

        import pyattest
        base = os.path.dirname(pyattest.__file__)
        with open(os.path.join(base, 'testutils', 'fixtures', 'root_cert.pem'), 'rb') as fh:
            return fh.read()

    def apple_settings(self, mode='enforce'):
        return self.settings(
            QUEST_ATTESTATION_MODE=mode,
            APPLE_APP_ID=self.APP_ID,
            APPLE_ATTEST_PRODUCTION=False,
            APPLE_ATTEST_ROOT_CA=self.apple_root_ca(),
        )

    # ── challenge lifecycle ──────────────────────────────────────────────────
    def test_challenge_is_single_use(self):
        from .attestation import consume_challenge

        nonce = self.get_challenge()
        self.assertIsNotNone(consume_challenge(self.user, nonce))
        # Second use refused — this is what stops an attestation being replayed.
        self.assertIsNone(consume_challenge(self.user, nonce))

    def test_challenge_is_bound_to_its_user(self):
        from .attestation import consume_challenge

        nonce = self.get_challenge()
        self.assertIsNone(consume_challenge(make_user('thief'), nonce))
        self.assertIsNotNone(consume_challenge(self.user, nonce))

    def test_expired_challenge_rejected(self):
        from datetime import timedelta as td

        from .attestation import consume_challenge
        from .models import AttestationChallenge

        nonce = self.get_challenge()
        AttestationChallenge.objects.filter(user=self.user, nonce=nonce).update(
            created_at=timezone.now() - td(hours=2),
        )
        self.assertIsNone(consume_challenge(self.user, nonce))

    # ── enforcement modes ────────────────────────────────────────────────────
    def test_audit_mode_records_missing_but_accepts(self):
        with self.settings(QUEST_ATTESTATION_MODE='audit'):
            res = self.submit()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(QuestProof.objects.get().attestation_status, 'missing')

    def test_off_mode_skips_entirely(self):
        with self.settings(QUEST_ATTESTATION_MODE='off'):
            res = self.submit()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(QuestProof.objects.get().attestation_status, 'skipped')

    def test_enforce_mode_blocks_unattested_submission(self):
        with self.settings(QUEST_ATTESTATION_MODE='enforce'):
            res = self.submit()
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(QuestProof.objects.count(), 0)

    def test_enforce_mode_blocks_forged_token(self):
        with self.settings(QUEST_ATTESTATION_MODE='enforce'):
            nonce = self.get_challenge()
            res = self.submit(
                attestation_platform='android',
                attestation_nonce=nonce,
                attestation_token='not-a-real-integrity-token',
            )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(QuestProof.objects.count(), 0)

    @staticmethod
    def fake_play_keys():
        """Structurally valid Play Integrity response keys, so config building
        succeeds and the *token* is what fails verification."""
        import base64

        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )

        public_der = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            'PLAY_INTEGRITY_DECRYPTION_KEY': base64.b64encode(b'\x00' * 32).decode(),
            'PLAY_INTEGRITY_VERIFICATION_KEY': base64.b64encode(public_der).decode(),
        }

    def test_audit_mode_records_failure_without_blocking(self):
        """A failed attestation still reaches the moderator as a signal."""
        with self.settings(QUEST_ATTESTATION_MODE='audit', **self.fake_play_keys()):
            nonce = self.get_challenge()
            res = self.submit(
                attestation_platform='android',
                attestation_nonce=nonce,
                attestation_token='not-a-real-integrity-token',
            )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        proof = QuestProof.objects.get()
        self.assertEqual(proof.attestation_status, 'failed')
        self.assertEqual(proof.attestation_platform, 'android')

    def test_enforce_mode_blocks_forged_token_with_keys_configured(self):
        with self.settings(QUEST_ATTESTATION_MODE='enforce', **self.fake_play_keys()):
            nonce = self.get_challenge()
            res = self.submit(
                attestation_platform='android',
                attestation_nonce=nonce,
                attestation_token='not-a-real-integrity-token',
            )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(QuestProof.objects.count(), 0)

    def test_replayed_nonce_fails_second_submission(self):
        from .attestation import consume_challenge

        with self.settings(QUEST_ATTESTATION_MODE='enforce'):
            nonce = self.get_challenge()
            consume_challenge(self.user, nonce)  # simulate the first use
            res = self.submit(
                attestation_platform='android',
                attestation_nonce=nonce,
                attestation_token='whatever',
            )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_unsupported_device_recorded_and_allowed_in_audit(self):
        with self.settings(QUEST_ATTESTATION_MODE='audit'):
            res = self.submit(attestation_platform='ios', attestation_unsupported='true')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(QuestProof.objects.get().attestation_status, 'unsupported')

    def test_challenge_endpoint_disabled_when_mode_off(self):
        with self.settings(QUEST_ATTESTATION_MODE='off'):
            res = self.client.post('/api/quests/attestation/challenge/')
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

    # ── real Apple App Attest verification ───────────────────────────────────
    def test_ios_key_registration_end_to_end(self):
        """Runs the genuine App Attest checks — CBOR decode, certificate chain,
        nonce binding, key-id and app-id match — against a test root CA."""
        import base64
        from hashlib import sha256

        from pyattest.testutils.factories.attestation import apple as apple_factory

        from .attestation import register_ios_key
        from .models import AttestedDevice

        with self.apple_settings():
            nonce = base64.b64decode(self.get_challenge())
            attestation, public_key = apple_factory.get(app_id=self.APP_ID, nonce=nonce)
            key_id = base64.b64encode(sha256(public_key).digest()).decode()

            result = register_ios_key(
                self.user, key_id, base64.b64encode(attestation).decode(), nonce,
            )

        self.assertTrue(result.ok, result.detail)
        device = AttestedDevice.objects.get(user=self.user)
        self.assertEqual(device.key_id, key_id)
        self.assertEqual(device.platform, 'ios')

    def test_ios_attestation_bound_to_its_challenge(self):
        """An attestation built for a different nonce must not verify."""
        import base64
        import secrets
        from hashlib import sha256

        from pyattest.testutils.factories.attestation import apple as apple_factory

        from .attestation import register_ios_key
        from .models import AttestedDevice

        with self.apple_settings():
            attestation, public_key = apple_factory.get(
                app_id=self.APP_ID, nonce=secrets.token_bytes(32),
            )
            result = register_ios_key(
                self.user,
                base64.b64encode(sha256(public_key).digest()).decode(),
                base64.b64encode(attestation).decode(),
                secrets.token_bytes(32),  # not the nonce it was built for
            )

        self.assertFalse(result.ok)
        self.assertEqual(AttestedDevice.objects.count(), 0)

    def test_ios_attestation_for_another_app_rejected(self):
        import base64
        import secrets
        from hashlib import sha256

        from pyattest.testutils.factories.attestation import apple as apple_factory

        from .attestation import register_ios_key

        with self.apple_settings():
            nonce = secrets.token_bytes(32)
            attestation, public_key = apple_factory.get(
                app_id='9999ZZZZZZ.com.someone.else', nonce=nonce,
            )
            result = register_ios_key(
                self.user,
                base64.b64encode(sha256(public_key).digest()).decode(),
                base64.b64encode(attestation).decode(),
                nonce,
            )

        self.assertFalse(result.ok)

    def test_assertion_from_unregistered_key_refused(self):
        import base64

        from .attestation import verify_ios_assertion

        with self.apple_settings():
            result = verify_ios_assertion(
                self.user, base64.b64encode(b'unknown-key').decode(), 'AAAA', b'nonce',
            )
        self.assertFalse(result.ok)
        self.assertIn('not registered', result.detail)

    # ── configuration safety ─────────────────────────────────────────────────
    def test_missing_play_keys_report_error_not_crash(self):
        from .attestation import verify_android

        with self.settings(
            PLAY_INTEGRITY_DECRYPTION_KEY='', PLAY_INTEGRITY_VERIFICATION_KEY='',
        ):
            result = verify_android('token', b'nonce')
        self.assertEqual(result.status, 'error')
        self.assertIn('not configured', result.detail)

    def test_misconfiguration_never_blocks_in_audit_mode(self):
        """Deploying with attestation half-configured must not stop uploads."""
        with self.settings(QUEST_ATTESTATION_MODE='audit', APPLE_APP_ID=''):
            nonce = self.get_challenge()
            res = self.submit(
                attestation_platform='ios',
                attestation_nonce=nonce,
                attestation_key_id='AAAA',
                attestation_assertion='AAAA',
            )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_moderator_sees_attestation_outcome(self):
        from django.contrib.auth.models import Group

        with self.settings(QUEST_ATTESTATION_MODE='audit'):
            self.submit()

        mod = make_user('attest_mod')
        group, _ = Group.objects.get_or_create(name='Moderator')
        mod.groups.add(group)
        self.client.force_authenticate(user=mod)

        proof_id = QuestProof.objects.get().pk
        row = self.client.get('/api/quests/moderation/proofs/').data['results'][0]
        self.assertEqual(row['attestation_status'], 'missing')

        detail = self.client.get(f'/api/quests/moderation/proofs/{proof_id}/').data
        self.assertIn('attestation_detail', detail)
