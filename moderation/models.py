"""Reports on everything that is not a Swift, and the record of what was done.

Why this exists next to `swifts.SwiftReport` rather than replacing it: that
model is live, tested, throttled, indexed and already in front of moderators.
Migrating Swift reports into a generic table would be a data migration in
service of tidiness, and it would throw away the one thing a purpose-built
table gives you - a real foreign key to the row being reported.

So the split is: Swifts keep their own table, everything else shares this one,
and both speak `moderation.reasons`. `ModerationQueue` in `admin.py` is what
puts the two in front of a moderator as one list.

The design decision worth reading twice is `target_repr`. A report is evidence,
and evidence about user-generated content has a habit of deleting itself - the
author edits the comment, the story expires six hours later, the message is
deleted for both sides. Storing a snapshot of the text at the moment somebody
objected to it means the queue still means something tomorrow. It is the same
argument `SwiftVideo.deleted_at` makes for soft deletion, one level up.
"""
from django.conf import settings
from django.db import models

from .reasons import Action, Reason, State


class ContentReport(models.Model):
    """Somebody reported a user, a story, a comment or a message.

    Unique per (reporter, target_type, target_id): one person is one report,
    however many times they press it. This is not politeness - it is the whole
    of the defence against brigading, which works precisely on systems that
    count a repeat press as a second opinion.
    """

    class Target(models.TextChoices):
        USER = 'user', 'User account'
        STORY = 'story', 'Story'
        COMMENT = 'comment', 'Swift comment'
        MESSAGE = 'message', 'Direct message'
        CLUB_MESSAGE = 'club_message', 'Club message'
        #: A user's Zone Music — audio other people hear when they are near
        #: them, so user-generated content under App Review 1.2. The target id
        #: is the *owner's* user id: the track lives on their row, and a
        #: replaced track is a new thing to report.
        ZONE_AUDIO = 'zone_audio', 'Zone music'

    class Source(models.TextChoices):
        """How the report was raised.

        `BLOCK` is the one that matters. App Review asked that blocking notify
        the developer, so a block from an abusive-content context files a report
        - but a moderator has to be able to tell "somebody pressed Report and
        chose a reason" apart from "somebody pressed Block", because the second
        is a much weaker signal and burying the first under it would make the
        queue useless.
        """

        REPORT = 'report', 'Reported'
        BLOCK = 'block', 'Raised by a block'
        AUTOMATED = 'automated', 'Flagged automatically'

    target_type = models.CharField(max_length=14, choices=Target.choices, db_index=True)
    #: The primary key of the reported row. Not a ForeignKey: it points at five
    #: different tables, and the alternative - a GenericForeignKey - buys an
    #: admin convenience at the cost of a contenttypes join on every queue read.
    #: `target_repr` and `target_user` below are what a moderator actually needs.
    target_id = models.PositiveIntegerField()

    #: Who is answerable for the reported thing. Denormalised on purpose: it is
    #: how a moderator gets from a report to "suspend this account" in one step,
    #: and how repeat offenders become visible at all. Null only if the account
    #: was deleted before the report was reviewed.
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reports_against',
    )
    #: The content as it stood when somebody objected to it. See the module
    #: docstring: without this the queue empties itself.
    target_repr = models.TextField(blank=True, default='')

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='content_reports',
    )
    reason = models.CharField(max_length=14, choices=Reason.choices)
    note = models.CharField(max_length=500, blank=True, default='')
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.REPORT, db_index=True,
    )

    state = models.CharField(
        max_length=8, choices=State.choices, default=State.OPEN, db_index=True,
    )
    #: Who closed it, and when, and what they did. Three columns rather than a
    #: free-text resolution note because an audit asks all three separately.
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='content_reports_reviewed',
    )
    action_taken = models.CharField(
        max_length=17, choices=Action.choices, blank=True, default=Action.NONE,
    )
    moderator_note = models.CharField(max_length=500, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['reporter', 'target_type', 'target_id'],
                name='unique_content_report',
            ),
        ]
        indexes = [
            # The queue: open reports, oldest first. A queue sorted newest first
            # quietly buries its own backlog.
            models.Index(fields=['state', 'created_at'], name='content_report_queue_idx'),
            # "Show me everything reported about this account."
            models.Index(fields=['target_user', '-created_at'], name='content_report_user_idx'),
            # "Show me every report about this one row."
            models.Index(
                fields=['target_type', 'target_id', '-created_at'],
                name='content_report_target_idx',
            ),
        ]

    def __str__(self):
        return f'{self.get_target_type_display()} #{self.target_id} ({self.reason})'


class ModerationAction(models.Model):
    """What a moderator did, as its own row.

    `ContentReport.action_taken` records the outcome of one report. This records
    the act, which is not the same thing: a suspension usually answers several
    reports at once, and a takedown sometimes answers none because a moderator
    found it while looking at something else. An enforcement history that only
    exists as a column on reports cannot show either.

    Append-only. `admin.py` refuses adds, changes and deletions - an editable
    audit is not one.
    """

    class Kind(models.TextChoices):
        CONTENT_REMOVED = 'content_removed', 'Content removed'
        CONTENT_RESTORED = 'content_restored', 'Content restored'
        USER_SUSPENDED = 'user_suspended', 'User suspended'
        USER_BANNED = 'user_banned', 'User banned'
        USER_REINSTATED = 'user_reinstated', 'User reinstated'
        REPORT_DISMISSED = 'report_dismissed', 'Report dismissed'

    kind = models.CharField(max_length=17, choices=Kind.choices, db_index=True)
    #: The account the action was taken against, when there is one.
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_actions',
    )
    #: Null when the action was taken by a management command or a rule rather
    #: than a person at the admin.
    moderator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_actions_taken',
    )
    detail = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['subject', '-created_at'], name='mod_action_subject_idx'),
        ]

    def __str__(self):
        who = self.subject.username if self.subject else 'content'
        return f'{self.get_kind_display()}: {who}'
