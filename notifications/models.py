from django.db import models
from django.conf import settings


class Notification(models.Model):
    class Type(models.TextChoices):
        NEARBY_USER = 'nearby_user', 'Nearby User'
        STORY_REACTION = 'story_reaction', 'Story Reaction'
        STORY_REPLY = 'story_reply', 'Story Reply'
        # Someone walked into earshot of a zone's music, or theirs came into
        # earshot of you. Deliberately *not* NEARBY_USER: proximity alone is
        # not news and the client drops those on arrival, but a track you can
        # only hear while you are standing here expires when you walk away.
        ZONE_MUSIC = 'zone_music', 'Zone Music'
        # Club activity. Chat itself is deliberately absent: a club message
        # pushes to the phone but writes no row, because the club's own chat is
        # already the list of club messages and a second copy in the alerts
        # feed would bury everything else in it. The three below have no other
        # home — a meetup proposed while you were away, the verdict on one you
        # proposed, and being added to a club you never asked to join.
        CLUB_EVENT = 'club_event', 'Club Event'
        CLUB_EVENT_STATUS = 'club_event_status', 'Club Event Decision'
        CLUB_INVITE = 'club_invite', 'Club Invite'
        # Engagement on your own Swift. Three types rather than one because
        # they open different things — a like opens the Swift, a comment opens
        # its thread — and because `notifications.utils` coalesces repeats per
        # (sender, subject, type) and a like must not overwrite a comment.
        SWIFT_LIKE = 'swift_like', 'Swift Like'
        SWIFT_COMMENT = 'swift_comment', 'Swift Comment'
        SWIFT_REPOST = 'swift_repost', 'Swift Repost'
        # This account was signed in to on a device it had not been seen on
        # before. The one alert here with no sender — it is about the account
        # itself rather than about something a person did to you, so `sender`
        # stays null and the message names the device instead.
        NEW_DEVICE = 'new_device', 'New Device Sign-In'

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
    # What the notification is about, when that is a club or a meetup. Same job
    # `story` does above and the same cascade: deleting a club or an event
    # takes its alerts with it rather than leaving rows that open nothing.
    club = models.ForeignKey(
        'clubs.Club',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    event = models.ForeignKey(
        'clubs.ClubEvent',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
    )
    # What the notification is about, when that is a Swift. Same job `story`
    # does above and the same cascade — though a Swift is soft-deleted rather
    # than removed, so in practice these outlive the Swift going away and the
    # alert list filters them out by joining to a live row.
    swift = models.ForeignKey(
        'swifts.SwiftVideo',
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
        indexes = [
            # Badge polls need unread rows only, regardless of read history.
            models.Index(
                fields=['recipient'], condition=models.Q(is_read=False),
                name='notification_unread_idx',
            ),
            # The alerts list: this user's notifications, newest first. The FK
            # index alone finds the rows but leaves the sort to be done in
            # memory, which is the expensive half for a busy account.
            models.Index(
                fields=['recipient', '-created_at'],
                name='notification_recipient_idx',
            ),
            # The cooldown checks — nearby-match and zone-music both ask
            # "has this pair had one of these lately".
            models.Index(
                fields=['recipient', 'sender', 'notification_type', 'created_at'],
                name='notification_cooldown_idx',
            ),
        ]

    def __str__(self):
        return f'{self.notification_type} → {self.recipient}'
