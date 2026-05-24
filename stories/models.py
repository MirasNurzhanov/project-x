from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import timedelta


class Story(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stories')
    content = models.TextField(max_length=280)
    image = models.ImageField(upload_to='stories/', null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    place_name = models.CharField(max_length=200, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    likes_count = models.IntegerField(default=0)

    def save(self, *args, **kwargs):
        if not self.pk:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} — {self.created_at:%Y-%m-%d %H:%M}"


class StoryReaction(models.Model):
    class ReactionType(models.TextChoices):
        LIKE = 'like', 'Like'
        FUNNY = 'funny', 'Funny'
        HELPFUL = 'helpful', 'Helpful'
        INTERESTING = 'interesting', 'Interesting'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='story_reactions')
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='reactions')
    reaction = models.CharField(max_length=20, choices=ReactionType.choices)

    class Meta:
        unique_together = ('user', 'story')

    def __str__(self):
        return f"{self.user} → {self.reaction} on story {self.story_id}"
