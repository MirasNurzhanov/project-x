from datetime import timedelta

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone

from moderation.models import ContentReport, ModerationAction
from moderation.reasons import State
from .models import User


class ReportsAgainstInline(admin.TabularInline):
    """Everything anybody has said about this account, on the account page.

    The single most useful thing when deciding whether to suspend somebody is
    whether this is the first complaint or the ninth, and that answer should not
    require going to another screen and searching.
    """

    model = ContentReport
    fk_name = 'target_user'
    extra = 0
    can_delete = False
    fields = ('created_at', 'target_type', 'reason', 'source', 'state', 'target_repr')
    readonly_fields = fields
    ordering = ('-created_at',)
    verbose_name_plural = 'Reports against this account'

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        'username', 'email', 'age_tier', 'enforcement', 'open_reports',
        'location_sharing', 'is_private', 'date_joined',
    )
    list_filter = (
        'suspended_at', 'age_tier', 'location_sharing', 'is_private', 'is_staff',
    )
    inlines = [ReportsAgainstInline]
    actions = ('action_suspend_7d', 'action_ban', 'action_reinstate')
    fieldsets = UserAdmin.fieldsets + (
        ('Profile', {'fields': ('bio', 'date_of_birth', 'age_tier', 'profile_picture')}),
        ('Location', {'fields': ('location_sharing', 'latitude', 'longitude')}),
        ('Profile privacy', {
            'fields': (
                'is_private',
                'visibility_interests', 'visibility_stories', 'visibility_stats',
                'visibility_zone_audio', 'visibility_connections',
            ),
        }),
        ('Discoverability', {
            'fields': ('discoverable_by_username', 'discoverable_by_interest'),
        }),
        ('Agreement', {
            'fields': ('terms_version', 'terms_accepted_at'),
            'description': (
                'Read-only. Written by the server on the paths that actually '
                'showed somebody the document - see <code>users/terms.py</code>. '
                'A blank version means the account has not agreed to the '
                'current text and meets the gate on next launch.'
            ),
        }),
        ('Enforcement', {
            'fields': ('suspended_at', 'suspended_until', 'suspension_reason'),
            'description': (
                "A suspension takes effect on the account's <em>next request</em>, "
                'not when its token expires - see '
                '<code>users/authentication.py</code>. Blank '
                '<strong>suspended until</strong> on a suspended account means '
                'permanent. Prefer the list actions, which write an audit row.'
            ),
        }),
    )
    readonly_fields = ('terms_version', 'terms_accepted_at')

    @admin.display(description='Standing')
    def enforcement(self, obj):
        if not obj.is_suspended():
            return 'OK' if obj.suspended_at is None else 'Suspension lapsed'
        return 'Banned' if obj.suspended_until is None else f'Suspended to {obj.suspended_until:%Y-%m-%d}'

    @admin.display(description='Open reports')
    def open_reports(self, obj):
        return ContentReport.objects.filter(
            target_user=obj, state=State.OPEN,
        ).count() or ''

    def _suspend(self, request, queryset, days, kind, verb):
        now = timezone.now()
        for user in queryset:
            user.suspended_at = now
            user.suspended_until = now + timedelta(days=days) if days else None
            user.suspension_reason = (
                user.suspension_reason or 'Violation of the Aura Terms of Use.'
            )
            user.save(update_fields=[
                'suspended_at', 'suspended_until', 'suspension_reason',
            ])
            ModerationAction.objects.create(
                kind=kind, subject=user, moderator=request.user,
                detail=f'{verb} from the user admin',
            )
        self.message_user(request, f'{verb} {queryset.count()} account(s).')

    @admin.action(description='Suspend for 7 days')
    def action_suspend_7d(self, request, queryset):
        self._suspend(
            request, queryset, 7, ModerationAction.Kind.USER_SUSPENDED, 'Suspended')

    @admin.action(description='Ban permanently')
    def action_ban(self, request, queryset):
        self._suspend(
            request, queryset, None, ModerationAction.Kind.USER_BANNED, 'Banned')

    @admin.action(description='Reinstate - lift the suspension')
    def action_reinstate(self, request, queryset):
        for user in queryset:
            user.suspended_at = None
            user.suspended_until = None
            user.suspension_reason = ''
            user.save(update_fields=[
                'suspended_at', 'suspended_until', 'suspension_reason',
            ])
            ModerationAction.objects.create(
                kind=ModerationAction.Kind.USER_REINSTATED,
                subject=user, moderator=request.user,
                detail='Reinstated from the user admin',
            )
        self.message_user(request, f'Reinstated {queryset.count()} account(s).')
