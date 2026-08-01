from django.contrib import admin

from .models import Region


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ('name', 'display_name', 'country_code', 'admin_level', 'point_count', 'fetched_at')
    list_filter = ('country_code', 'admin_level')
    search_fields = ('name', 'display_name')
    # The rings are a cached artefact of the upstream fetch, not something to
    # hand-edit — a malformed ring would silently break containment tests.
    readonly_fields = ('rings', 'point_count', 'fetched_at')
