from django.db import models
from django.conf import settings


class Quest(models.Model):
    class QuestType(models.TextChoices):
        DAILY = 'daily', 'Daily'
        WEEKLY = 'weekly', 'Weekly'

    class ConditionType(models.TextChoices):
        SEND_HI = 'send_hi', 'Send Hi'
        SEND_MESSAGE = 'send_message', 'Send Message'
        UPDATE_LOCATION = 'update_location', 'Update Location'
        MAKE_FRIEND = 'make_friend', 'Make Friend'
        SELECT_INTERESTS = 'select_interests', 'Select Interests'
        HAVE_ENCOUNTER = 'have_encounter', 'Have Encounter'

    title = models.CharField(max_length=120)
    description = models.TextField()
    reward_points = models.PositiveIntegerField()
    quest_type = models.CharField(max_length=10, choices=QuestType.choices)
    condition_type = models.CharField(max_length=20, choices=ConditionType.choices)

    def __str__(self):
        return f'[{self.quest_type}] {self.title}'


class UserQuest(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_quests',
    )
    quest = models.ForeignKey(Quest, on_delete=models.CASCADE, related_name='user_quests')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    reset_date = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'quest')

    def __str__(self):
        return f'{self.user} — {self.quest.title} ({"done" if self.completed else "pending"})'


class UserAuraPoints(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='aura_points',
    )
    total_points = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'{self.user}: {self.total_points} pts'
