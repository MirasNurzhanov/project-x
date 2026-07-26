from datetime import timedelta

from django.utils import timezone

from .models import PointEvent, Quest, UserQuest


def _is_reset_due(user_quest):
    if not user_quest.completed or user_quest.reset_date is None:
        return True
    today = timezone.localdate()
    if user_quest.quest.quest_type == Quest.QuestType.DAILY:
        return user_quest.reset_date < today
    week_start = today - timedelta(days=today.weekday())
    return user_quest.reset_date < week_start


def check_and_complete_quest(user, condition_type):
    print(f'[quest] check_and_complete_quest called: user={user} condition_type={condition_type}')
    # interest__isnull=True excludes the per-interest meet_friend_in_person rows.
    # Ordered .first() rather than .get(): several quests may share a condition
    # type, and a MultipleObjectsReturned here would 500 the caller's action
    # (sending a Hi, posting a story) — never worth it for a side effect.
    quest = (
        Quest.objects
        .filter(condition_type=condition_type, interest__isnull=True)
        .order_by('pk')
        .first()
    )
    if quest is None:
        print(f'[quest] no quest found for condition_type={condition_type}')
        return

    user_quest, _ = UserQuest.objects.get_or_create(user=user, quest=quest)

    if not _is_reset_due(user_quest):
        return

    now = timezone.now()
    user_quest.completed = True
    user_quest.completed_at = now
    user_quest.reset_date = timezone.localdate()
    user_quest.save(update_fields=['completed', 'completed_at', 'reset_date'])

    # period_start=None: these completions are inferred from app events, not
    # photo-verified, so they count toward the all-time total but never toward
    # the weekly leaderboard.
    from .ledger import record_points
    record_points(
        user, quest.reward_points, PointEvent.Reason.UNVERIFIED_QUEST,
        quest=quest, period_start=None,
    )
    print(f'[quest] awarded {quest.reward_points} pts to {user} for "{quest.title}"')
