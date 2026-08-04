from django.db import models
from django.conf import settings


class Club(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    interest = models.ForeignKey(
        'interests.Interest',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='clubs',
    )
    emblem = models.ImageField(upload_to='club_emblems/', null=True, blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_clubs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class ClubMember(models.Model):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        MEMBER = 'member', 'Member'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_memberships',
    )
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='members')
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'club')
        ordering = ['joined_at']

    def __str__(self):
        return f'{self.user} — {self.club} ({self.role})'


class ClubEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='events')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_events',
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location_name = models.CharField(max_length=200, blank=True)
    latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    event_datetime = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_club_events',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['event_datetime']

    def __str__(self):
        return f'{self.title} @ {self.club}'


class ClubMessage(models.Model):
    class MessageType(models.TextChoices):
        TEXT = 'text', 'Text'
        MEETUP_PROPOSAL = 'meetup_proposal', 'Meetup Proposal'

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='club_messages',
    )
    content = models.TextField(blank=True)
    image = models.ImageField(upload_to='club_messages/', null=True, blank=True)
    # Set when the client uploaded straight to Cloudinary and only the URL
    # passed through here. Takes precedence over `image` when both exist.
    image_url = models.URLField(max_length=500, blank=True, default='')
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    event = models.ForeignKey(
        ClubEvent,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='proposal_messages',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.sender} → {self.club}: {self.content[:40]}'
