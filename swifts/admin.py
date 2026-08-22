from django.contrib import admin

from .models import (
    SwiftComment, SwiftLike, SwiftLocationReveal, SwiftReport, SwiftRepost,
    SwiftTrack, SwiftVideo, SwiftView,
)


@admin.register(SwiftVideo)
class SwiftVideoAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'author', 'status', 'visibility', 'moderation',
        'location_shared', 'like_count', 'comment_count', 'view_count', 'created_at',
    )
    list_filter = ('status', 'visibility', 'moderation', 'location_shared', 'created_at')
    search_fields = ('author__username', 'caption', 'media_id')
    # Coordinates are deliberately absent from `list_display` and
    # `search_fields`: the admin is a legitimate place to see one while
    # handling a report, but a column of them in a list view turns a support
    # screen into a map of everybody who ever attached a location.
    readonly_fields = ('like_count', 'comment_count', 'repost_count', 'view_count')


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

    list_display = ('id', 'swift', 'reporter', 'reason', 'state', 'created_at')
    list_filter = ('state', 'reason', 'created_at')
    search_fields = ('reporter__username', 'note')
    ordering = ('state', 'created_at')
    readonly_fields = ('swift', 'reporter', 'reason', 'note', 'created_at')


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
                'Every row here is Aura-owned, licensed, or open — nothing is '
                'scraped from a streaming service. Imported tracks arrive '
                '<strong>inactive</strong> and stay that way until somebody '
                'here has read the licence and published them.'
            ),
        }),
        ('Publication', {'fields': ('is_active', 'priority', 'use_count', 'created_at')}),
    )
