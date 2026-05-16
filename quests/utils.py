from datetime import timedelta

from django.utils import timezone

from .models import Quest, UserAuraPoints, UserQuest


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
    try:
        quest = Quest.objects.get(condition_type=condition_type)
    except Quest.DoesNotExist:
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

    aura, _ = UserAuraPoints.objects.get_or_create(user=user)
    aura.total_points += quest.reward_points
    aura.save(update_fields=['total_points'])
