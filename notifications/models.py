from django.db import models
from django.conf import settings


class Notification(models.Model):
    class Type(models.TextChoices):
        NEARBY_USER = 'nearby_user', 'Nearby User'
        STORY_REACTION = 'story_reaction', 'Story Reaction'
        STORY_REPLY = 'story_reply', 'Story Reply'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_notifications',
    )
    # What the notification is *about*, when that is a story. This is what makes
    # a story alert traceable — without it the client can say "someone liked
    # your story" but cannot open the story that was liked. Cascades, so
    # deleting a story takes its alerts with it rather than leaving rows
    # pointing at nothing.
    story = models.ForeignKey(
        'stories.Story',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    notification_type = models.CharField(max_length=20, choices=Type.choices)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.notification_type} → {self.recipient}'
