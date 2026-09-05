"""The moderation desk.

Django Admin, not a bespoke frontend. What a moderator needs is a queue sorted
oldest-first, the reported text in front of them, and four buttons - remove,
suspend, ban, dismiss. Admin gives all of that for the cost of this file, and a
custom tool would need auth, permissions, an audit trail and a deploy target
before it did anything Admin does not.

Everything destructive here goes through `moderation.services` or writes a
`ModerationAction` row, so there is no path to a takedown or a suspension that
leaves no record of who did it.
"""
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from . import services
from .models import ContentReport, ModerationAction
from .reasons import URGENT_REASONS, Action, Moderation, State


def _truncate(text, length=120):
    text = (text or '').replace('\n', ' ').strip()
    return text if len(text) <= length else text[: length - 1] + '…'


@admin.register(ContentReport)
class ContentReportAdmin(admin.ModelAdmin):
    """The queue for users, stories, comments and messages.

    Defaults to open reports, oldest first - the thing that has been waiting
    longest is the thing that should be looked at, and a queue sorted newest
    first quietly buries its own backlog. Same ordering, and the same argument,
    as `swifts.admin.SwiftReportAdmin`.
    """

    list_display = (
        'id', 'urgency', 'target_type', 'target_id', 'target_user',
        'reason', 'source', 'evidence', 'state', 'created_at',
    )
    list_filter = ('state', 'source', 'target_type', 'reason', 'created_at')
    search_fields = (
        'reporter__username', 'target_user__username', 'note', 'target_repr',
    )
    ordering = ('state', 'created_at')
    date_hierarchy = 'created_at'
    list_select_related = ('target_user', 'reporter', 'reviewed_by')

    #: Everything a reporter said is evidence and none of it is editable. Only
    #: the verdict fields below can be written here.
    readonly_fields = (
        'target_type', 'target_id', 'target_user', 'target_repr',
        'reporter', 'reason', 'note', 'source', 'created_at',
        'reviewed_at', 'reviewed_by', 'other_reports',
    )
    fieldsets = (
        ('What was reported', {
            'fields': (
                'target_type', 'target_id', 'target_user', 'target_repr',
                'other_reports',
            ),
            'description': (
                '<strong>Target repr</strong> is a snapshot taken when the '
                'report was filed. The live row may since have been edited or '
                'deleted - that is why the snapshot exists.'
            ),
        }),
        ('Who reported it', {'fields': ('reporter', 'reason', 'note', 'source', 'created_at')}),
        ('Verdict', {
            'fields': ('state', 'action_taken', 'moderator_note', 'reviewed_at', 'reviewed_by'),
            'description': (
                'Use the list actions to remove content or suspend an account - '
                'they write an audit row. Editing <strong>state</strong> here '
                'closes the report without taking any action against the '
                'content or the user.'
            ),
        }),
    )

    actions = (
        'action_remove_content',
        'action_restore_content',
        'action_suspend_7d',
        'action_ban',
        'action_dismiss',
    )

    @admin.display(description='!', ordering='reason')
    def urgency(self, obj):
        """A visible marker on the categories that should not wait.

        Colour alone would not survive a screenshot into a ticket, so it is a
        glyph with a title attribute rather than a coloured cell.
        """
        if obj.reason in URGENT_REASONS:
            return format_html(
                '<span title="Urgent category" class="ax-flag">▲</span>'
            )
        return ''

    @admin.display(description='Reported content')
    def evidence(self, obj):
        return _truncate(obj.target_repr, 80) or '—'

    @admin.display(description='Other open reports on this target')
    def other_reports(self, obj):
        n = ContentReport.objects.filter(
            target_type=obj.target_type, target_id=obj.target_id, state=State.OPEN,
        ).exclude(pk=obj.pk).count()
        if not n:
            return 'None'
        return f'{n} other open report(s) - likely the same incident'

    # ── Actions ───────────────────────────────────────────────────────────────
    def _set_content_moderation(self, request, queryset, moderation, action, kind, verb):
        touched = 0
        for report in queryset:
            instance, author, _ = services.resolve_target(report.target_type, report.target_id)
            if instance is None or not hasattr(instance, 'moderation'):
                continue
            type(instance).objects.filter(pk=instance.pk).update(moderation=moderation)
            ModerationAction.objects.create(
                kind=kind,
                subject=author,
                moderator=request.user,
                detail=f'{report.target_type} #{report.target_id} via report #{report.pk}',
            )
            services.resolve_report(
                report, request.user, state=State.ACTIONED, action=action,
            )
            touched += 1
        self.message_user(
            request, f'{verb} {touched} item(s).',
            messages.SUCCESS if touched else messages.WARNING,
        )

    @admin.action(description='Remove the reported content')
    def action_remove_content(self, request, queryset):
        self._set_content_moderation(
            request, queryset, Moderation.REMOVED, Action.CONTENT_REMOVED,
            ModerationAction.Kind.CONTENT_REMOVED, 'Removed',
        )

    @admin.action(description='Restore the content (report rejected)')
    def action_restore_content(self, request, queryset):
        self._set_content_moderation(
            request, queryset, Moderation.OK, Action.CONTENT_RESTORED,
            ModerationAction.Kind.CONTENT_RESTORED, 'Restored',
        )

    def _suspend(self, request, queryset, days, action, kind, verb):
        from datetime import timedelta

        touched = 0
        for report in queryset:
            user = report.target_user
            if user is None:
                continue
            user.suspended_at = timezone.now()
            user.suspended_until = (
                timezone.now() + timedelta(days=days) if days else None
            )
            user.suspension_reason = (
                f'{report.get_reason_display()} (report #{report.pk})'
            )[:200]
            user.save(update_fields=[
                'suspended_at', 'suspended_until', 'suspension_reason',
            ])
            ModerationAction.objects.create(
                kind=kind, subject=user, moderator=request.user,
                detail=f'{verb} via report #{report.pk}',
            )
            services.resolve_report(
                report, request.user, state=State.ACTIONED, action=action,
            )
            touched += 1
        self.message_user(
            request, f'{verb} {touched} account(s).',
            messages.SUCCESS if touched else messages.WARNING,
        )

    @admin.action(description='Suspend the reported account for 7 days')
    def action_suspend_7d(self, request, queryset):
        self._suspend(
            request, queryset, 7, Action.USER_SUSPENDED,
            ModerationAction.Kind.USER_SUSPENDED, 'Suspended',
        )

    @admin.action(description='Ban the reported account permanently')
    def action_ban(self, request, queryset):
        self._suspend(
            request, queryset, None, Action.USER_BANNED,
            ModerationAction.Kind.USER_BANNED, 'Banned',
        )

    @admin.action(description='Dismiss - nothing wrong here')
    def action_dismiss(self, request, queryset):
        for report in queryset:
            services.resolve_report(report, request.user, state=State.REVIEWED)
        self.message_user(request, f'Dismissed {queryset.count()} report(s).')

    def has_add_permission(self, request):
        # Reports come from users and from the filter, never from this screen.
        return False


@admin.register(ModerationAction)
class ModerationActionAdmin(admin.ModelAdmin):
    """The enforcement log. Read-only on purpose - an editable audit is not one.

    Same treatment, and the same one-line justification, as
    `swifts.admin.SwiftLocationRevealAdmin`.
    """

    list_display = ('id', 'created_at', 'kind', 'subject', 'moderator', 'detail')
    list_filter = ('kind', 'created_at')
    search_fields = ('subject__username', 'moderator__username', 'detail')
    date_hierarchy = 'created_at'
    list_select_related = ('subject', 'moderator')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
