from django.contrib import admin

from .models import Region


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'display_name', 'country_code', 'kind', 'admin_level',
        'source', 'verified', 'point_count', 'fetched_at',
    )
    list_filter = ('source', 'verified', 'kind', 'country_code', 'admin_level')
    search_fields = ('name', 'display_name', 'parent_name')
    # The rings are a cached artefact of the upstream fetch, not something to
    # hand-edit — a malformed ring would silently break containment tests.
    readonly_fields = ('rings', 'point_count', 'fetched_at', 'submitted_by')
    actions = ('verify_regions',)

    @admin.action(description='Mark selected boundaries as verified')
    def verify_regions(self, request, queryset):
        """Promote a hand-traced boundary from "its author's" to "everyone's".

        This is the only way an unverified region becomes visible to other
        people, and it is a human decision on purpose: a traced outline is
        somebody's opinion about where a district ends until somebody else
        agrees, and one wrong one becomes every future lookup's answer.
        """
        updated = queryset.filter(verified=False).update(verified=True)
        self.message_user(request, f'{updated} boundaries are now shared.')
