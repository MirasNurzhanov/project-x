from django.contrib import admin
from django.utils.html import format_html

from moderation.reasons import Action

from .models import (
    SwiftComment, SwiftLike, SwiftLocationReveal, SwiftModerationJob,
    SwiftReport, SwiftRepost, SwiftTrack, SwiftVideo, SwiftView,
)


class SwiftModerationJobInline(admin.StackedInline):
    """The AI's evidence, on the Swift it is about.

    An inline rather than a screen of its own, and that is the whole of
    requirement 6: there is no second moderation dashboard. A moderator opening
    a Swift sees what the classifier saw — which timestamps, which categories,
    how sure, which model — in the place they were already going to look, and
    then presses the same four buttons they press for a human report.

    Read-only in full. This is evidence about a decision, and an editable
    record of what a classifier concluded is not one.
    """

    model = SwiftModerationJob
    extra = 0
    can_delete = False
    verbose_name = 'AI moderation history'
    verbose_name_plural = 'AI moderation history'
    readonly_fields = (
        'result_explanation', 'state', 'outcome', 'basis', 'categories', 'confidence',
        'sampled_ms', 'frames_classified', 'frames_requested', 'model_used',
        'audio_checked', 'attempts', 'next_attempt_at', 'last_error',
        'report', 'created_at', 'finished_at',
    )
    fields = readonly_fields

    @admin.display(description='About this result')
    def result_explanation(self, obj):
        return (
            'Saved results from the last AI run. Human review does not change '
            'these results or retrain the model. The Swift Moderation field '
            'shows the current decision. Confidence is the model\'s own estimate, '
            'not a measured probability of a policy violation.'
        )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SwiftVideo)
class SwiftVideoAdmin(admin.ModelAdmin):
    inlines = (SwiftModerationJobInline,)
    list_display = (
        'id', 'author', 'status', 'visibility', 'moderation', 'ai_verdict',
        'location_shared', 'like_count', 'comment_count', 'view_count', 'created_at',
    )
    list_filter = ('moderation', 'status', 'visibility', 'location_shared', 'created_at')
    # `moderation` leads the filter list because "show me everything waiting on
    # a decision" is the question this screen is opened to answer. Quarantined
    # Swifts have no report row - the filter is what surfaces them, so it has
    # to be the first thing on the page.
    list_editable = ('moderation',)
    search_fields = ('author__username', 'caption', 'media_id')
    # Coordinates are deliberately absent from `list_display` and
    # `search_fields`: the admin is a legitimate place to see one while
    # handling a report, but a column of them in a list view turns a support
    # screen into a map of everybody who ever attached a location.
    readonly_fields = ('like_count', 'comment_count', 'repost_count', 'view_count')

    def get_queryset(self, request):
        # One join instead of one query per row for the column below. The
        # moderation list view is the screen most likely to be opened on a
        # backlog of a few hundred rows.
        return super().get_queryset(request).select_related('moderation_job__report')

    @admin.display(description='AI result (saved)')
    def ai_verdict(self, obj):
        """One glance at what the classifier concluded, if anything.

        Blank for every Swift posted before this feature and every Swift posted
        while it is switched off, which is the honest answer: "not screened" and
        "screened and found clean" must not look the same in a queue.
        """
        job = getattr(obj, 'moderation_job', None)
        if job is None:
            return ''
        report = job.report
        human_approved = (
            obj.moderation == SwiftVideo.Moderation.OK
            and report is not None
            and report.state == SwiftReport.State.REVIEWED
            and report.reviewed_by_id is not None
            and report.reviewed_at is not None
            and report.action_taken == Action.CONTENT_RESTORED
        )
        if human_approved:
            return format_html(
                '<span style="color:#146c2e" title="{}">Human approved</span>',
                'The reviewer restored this Swift. The original AI result '
                'is preserved in AI moderation history.',
            )
        if job.state in (SwiftModerationJob.State.QUEUED,
                         SwiftModerationJob.State.RUNNING):
            return format_html('<span title="Waiting on the classifier">…</span>')
        if job.outcome == 'approved':
            return format_html('<span style="color:#146c2e" title="AI: clean">ok</span>')
        if job.state == SwiftModerationJob.State.FAILED:
            return 'Failed - needs review'
        detail = job.categories or job.basis or 'flagged'
        if obj.moderation == SwiftVideo.Moderation.OK:
            return format_html(
                '<span title="{}">Approved; earlier AI flag ({})</span>',
                detail, f'{job.confidence or 0:.0%}',
            )
        return format_html(
            '<span style="color:#b3261e;font-weight:700" title="{}">{}</span>',
            detail, f'{job.confidence or 0:.0%}',
        )


@admin.register(SwiftComment)
class SwiftCommentAdmin(admin.ModelAdmin):
    list_display = ('id', 'swift', 'author', 'parent', 'deleted_at', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('author__username', 'text')


@admin.register(SwiftLike)
class SwiftLikeAdmin(admin.ModelAdmin):
    list_display = ('id', 'swift', 'user', 'created_at')


@admin.register(SwiftRepost)
class SwiftRepostAdmin(admin.ModelAdmin):
    list_display = ('id', 'swift', 'user', 'created_at')


@admin.register(SwiftView)
class SwiftViewAdmin(admin.ModelAdmin):
    list_display = ('id', 'swift', 'viewer', 'view_count', 'watch_ms',
                    'completed', 'first_viewed_at')


@admin.register(SwiftLocationReveal)
class SwiftLocationRevealAdmin(admin.ModelAdmin):
    """The audit. Read-only on purpose — an editable audit is not one."""

    list_display = ('id', 'swift', 'viewer', 'reveal_count', 'revealed_at')
    list_filter = ('revealed_at',)
    search_fields = ('viewer__username',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SwiftReport)
class SwiftReportAdmin(admin.ModelAdmin):
    """The moderation queue.

    Defaults to open reports, oldest first — the thing that has been waiting
    longest is the thing that should be looked at, and a queue sorted newest
    first quietly buries its own backlog.
    """

    list_display = (
        'id', 'swift', 'reporter', 'source', 'reason', 'state', 'action_taken',
        'created_at',
    )
    # `source` is in the filter list because the first question a moderator
    # asks a mixed queue is which of these a person actually complained about.
    # An AI item and three independent reports are very different evidence and
    # a queue that cannot separate them makes the moderator do it by eye.
    list_filter = ('state', 'source', 'reason', 'action_taken', 'created_at')
    search_fields = ('reporter__username', 'note')
    ordering = ('state', 'created_at')
    readonly_fields = (
        'swift', 'reporter', 'source', 'reason', 'note', 'created_at',
        'reviewed_at', 'reviewed_by',
    )
    actions = ('action_remove_swift', 'action_restore_swift',
               'action_suspend_author_7d', 'action_ban_author', 'action_dismiss')

    def _close(self, request, report, state, action):
        from moderation import services as moderation_services

        moderation_services.resolve_report(
            report, request.user, state=state, action=action)

    @admin.action(description='Take the Swift down')
    def action_remove_swift(self, request, queryset):
        from moderation.models import ModerationAction
        from moderation.reasons import Action, State

        for report in queryset.select_related('swift'):
            SwiftVideo.objects.filter(pk=report.swift_id).update(
                moderation=SwiftVideo.Moderation.REMOVED)
            ModerationAction.objects.create(
                kind=ModerationAction.Kind.CONTENT_REMOVED,
                subject=report.swift.author, moderator=request.user,
                detail=f'Swift #{report.swift_id} via report #{report.pk}',
            )
            self._close(request, report, State.ACTIONED, Action.CONTENT_REMOVED)
        self.message_user(request, f'Removed {queryset.count()} Swift(s).')

    @admin.action(description='Restore the Swift (report rejected)')
    def action_restore_swift(self, request, queryset):
        from moderation.models import ModerationAction
        from moderation.reasons import Action, State

        for report in queryset.select_related('swift'):
            SwiftVideo.objects.filter(pk=report.swift_id).update(
                moderation=SwiftVideo.Moderation.OK)
            ModerationAction.objects.create(
                kind=ModerationAction.Kind.CONTENT_RESTORED,
                subject=report.swift.author, moderator=request.user,
                detail=f'Swift #{report.swift_id} via report #{report.pk}',
            )
            self._close(request, report, State.REVIEWED, Action.CONTENT_RESTORED)
        self.message_user(request, f'Restored {queryset.count()} Swift(s).')

    def _suspend(self, request, queryset, days, action, kind, verb):
        from datetime import timedelta

        from django.utils import timezone

        from moderation.models import ModerationAction
        from moderation.reasons import State

        for report in queryset.select_related('swift__author'):
            author = report.swift.author
            author.suspended_at = timezone.now()
            author.suspended_until = (
                timezone.now() + timedelta(days=days) if days else None)
            author.suspension_reason = (
                f'{report.get_reason_display()} (Swift report #{report.pk})')[:200]
            author.save(update_fields=[
                'suspended_at', 'suspended_until', 'suspension_reason'])
            ModerationAction.objects.create(
                kind=kind, subject=author, moderator=request.user,
                detail=f'{verb} via Swift report #{report.pk}',
            )
            self._close(request, report, State.ACTIONED, action)
        self.message_user(request, f'{verb} {queryset.count()} account(s).')

    @admin.action(description='Suspend the author for 7 days')
    def action_suspend_author_7d(self, request, queryset):
        from moderation.models import ModerationAction
        from moderation.reasons import Action

        self._suspend(request, queryset, 7, Action.USER_SUSPENDED,
                      ModerationAction.Kind.USER_SUSPENDED, 'Suspended')

    @admin.action(description='Ban the author permanently')
    def action_ban_author(self, request, queryset):
        from moderation.models import ModerationAction
        from moderation.reasons import Action

        self._suspend(request, queryset, None, Action.USER_BANNED,
                      ModerationAction.Kind.USER_BANNED, 'Banned')

    @admin.action(description='Dismiss - nothing wrong here')
    def action_dismiss(self, request, queryset):
        from moderation.reasons import Action, State

        for report in queryset:
            self._close(request, report, State.REVIEWED, Action.NONE)
        self.message_user(request, f'Dismissed {queryset.count()} report(s).')


@admin.register(SwiftTrack)
class SwiftTrackAdmin(admin.ModelAdmin):
    """The music catalogue.

    `is_active` is the publish gate and it is in `list_editable` on purpose:
    publishing a track is a deliberate act by somebody who has read the
    licence, and making that one click from the list is what keeps the review
    step from being skipped.
    """

    list_display = ('id', 'title', 'artist', 'genre', 'source', 'provider',
                    'is_active', 'priority', 'use_count')
    list_filter = ('is_active', 'source', 'provider', 'genre')
    list_editable = ('is_active', 'priority')
    search_fields = ('title', 'artist', 'licence', 'attribution')
    readonly_fields = ('use_count', 'created_at', 'provider',
                       'provider_track_id', 'synced_at')
    fieldsets = (
        (None, {'fields': ('title', 'artist', 'genre', 'duration_ms')}),
        ('Media', {'fields': ('audio_url', 'cloudinary_id', 'artwork_url')}),
        ('Imported from', {
            'fields': ('provider', 'provider_track_id', 'synced_at'),
            'description': (
                'Read-only. Set by <code>manage.py sync_music</code>; blank for '
                'a track added by hand. A re-sync matches on these and updates '
                'the track in place — it never touches the publication fields '
                'below, so an upstream change cannot unpublish anything.'
            ),
        }),
        ('Rights', {
            'fields': ('source', 'licence', 'attribution'),
            'description': (
                'A track is not published until somebody has read the licence. '
                'Every row here is Approx-owned, licensed, or open — nothing is '
                'scraped from a streaming service. Imported tracks arrive '
                '<strong>inactive</strong> and stay that way until somebody '
                'here has read the licence and published them.'
            ),
        }),
        ('Publication', {'fields': ('is_active', 'priority', 'use_count', 'created_at')}),
    )
