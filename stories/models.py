from django.conf import settings
from django.db import models

from moderation.reasons import Moderation
from django.utils import timezone
from datetime import timedelta


class Story(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stories')
    # Optional, and the commonest story of all is the one without it: a photo
    # posted without commentary. This was required until 0009, which made a
    # picture-only story impossible to post at all — the client had to invent a
    # caption or refuse. A story with *neither* a caption nor an image is the
    # only thing that is not a story; CreateStoryView is what enforces that,
    # since it is a rule about the pair rather than about either field.
    content = models.TextField(max_length=280, blank=True, default='')
    image = models.ImageField(upload_to='stories/', null=True, blank=True)
    # Legacy: set when the client uploaded straight to Cloudinary, before the
    # R2 migration retired that path — every upload now goes through Django as
    # `image`. Still takes precedence over `image` on rows from that era, since
    # those never got one.
    image_url = models.URLField(max_length=500, blank=True, default='')
    # Null together, and null is the normal case rather than a defect: a story
    # posted while location sharing is Paused carries no position at all. The
    # two products are separate - a story is something you show your friends,
    # a position is something you publish to a map - and only the second one
    # answers to `location_sharing`. See stories/visibility.py, and
    # CreateStoryView, which is what actually enforces the null.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    place_name = models.CharField(max_length=200, blank=True)
    #: Screening state. Written by `moderation.services` - never by a view
    #: directly - so the rule about what is publishable lives in one place.
    #: `stories.visibility.readable_q` is what enforces it on read.
    moderation = models.CharField(
        max_length=11, choices=Moderation.choices, default=Moderation.OK, db_index=True,
    )
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    likes_count = models.IntegerField(default=0)
    # A favorite flag the author sets on their own story — a shortlist over
    # the full "Mine" archive. Not a bookmark of someone else's content: only
    # the author can ever star a story, so this lives directly on the row
    # rather than in a cross-user join table.
    is_starred = models.BooleanField(default=False)

    @property
    def is_placed(self):
        """Whether this story carries a position at all.

        Necessary but not sufficient for it to be drawn: a placed story whose
        author has since paused stays readable and stops being a map pin. That
        second half is `stories.visibility.can_show_on_map`.
        """
        return self.latitude is not None and self.longitude is not None

    def save(self, *args, **kwargs):
        if not self.pk:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The map's story pins: live stories inside a bounding box.
            # `expires_at` leads because it eliminates most of the table on its
            # own — a story lives 24 hours and the table does not.
            models.Index(
                fields=['expires_at', 'latitude', 'longitude'],
                name='story_live_box_idx',
            ),
            # The author's own archive, newest first.
            models.Index(fields=['user', '-created_at'], name='story_author_idx'),
        ]

    def __str__(self):
        return f"{self.user} — {self.created_at:%Y-%m-%d %H:%M}"


class StoryReaction(models.Model):
    class ReactionType(models.TextChoices):
        LIKE = 'like', 'Like'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='story_reactions')
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='reactions')
    reaction = models.CharField(max_length=20, choices=ReactionType.choices)

    class Meta:
        unique_together = ('user', 'story')

    def __str__(self):
        return f"{self.user} → {self.reaction} on story {self.story_id}"
