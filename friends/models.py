from django.conf import settings
from django.db import models
from core.media_storage import protected_media


class HiRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_hi_requests'
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_hi_requests'
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('sender', 'receiver')

    def __str__(self):
        return f"{self.sender} -> {self.receiver} ({self.status})"


class FriendRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_friend_requests'
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_friend_requests'
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('sender', 'receiver')

    def __str__(self):
        return f"{self.sender} -> {self.receiver} ({self.status})"


class BlockedUser(models.Model):
    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocking'
    )
    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocked_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('blocker', 'blocked')

    def __str__(self):
        return f"{self.blocker} blocked {self.blocked}"


class Message(models.Model):
    client_id = models.UUIDField(null=True, blank=True, editable=False)
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_messages'
    )
    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_messages'
    )
    content = models.TextField(blank=True)
    image = models.FileField(upload_to='chat_images/', storage=protected_media, null=True, blank=True)
    # Legacy: set when the client uploaded straight to Cloudinary, before the
    # R2 migration retired that path — every upload now goes through Django as
    # `image`. Still takes precedence over `image` on rows from that era, since
    # those never got one.
    image_url = models.URLField(max_length=500, blank=True, default='')
    #: A Swift sent into this conversation, when the message is a share.
    #:
    #: A foreign key rather than a link in `content`, because the two questions
    #: a shared Swift raises can only be answered by a join: *may this reader
    #: still watch it* — audience and blocks are re-checked at read time, not
    #: frozen at send time — and *is it still there*. A pasted URL would keep
    #: rendering a card for a Swift that has since been deleted or made
    #: private, and the chat would be the one surface in the app where that
    #: leaks.
    #:
    #: CASCADE: a hard-deleted Swift takes its share messages with it. Soft
    #: deletion — the ordinary case, see `SwiftVideo.delete` — leaves the row
    #: and `friends.serializers` stops rendering the card.
    #:
    #: `content` stays usable alongside it: a share with a note is one message,
    #: not two.
    swift = models.ForeignKey(
        'swifts.SwiftVideo', on_delete=models.CASCADE,
        null=True, blank=True, related_name='shares',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    deleted_for_sender = models.BooleanField(default=False)
    deleted_for_receiver = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['sender', 'receiver', 'client_id'], name='unique_dm_client_message',
            ),
        ]
        indexes = [
            # The unread badge, which every authenticated screen polls about
            # every ten seconds, and the per-sender breakdown beside it.
            models.Index(
                fields=['receiver', 'is_read', 'sender'],
                name='message_unread_idx',
            ),
            # One conversation in order, and the chat list's "latest message per
            # counterpart" walk, which reads the same index backwards.
            models.Index(
                fields=['sender', 'receiver', '-created_at'],
                name='message_thread_idx',
            ),
        ]

    def __str__(self):
        preview = self.content[:30] if self.content else '[image]'
        return f"{self.sender} -> {self.receiver}: {preview}"
