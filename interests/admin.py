from django.contrib import admin

from .models import Interest


@admin.register(Interest)
class InterestAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon', 'needs_review')
    list_filter = ('needs_review',)
    search_fields = ('name',)
    actions = ('approve_interests',)

    @admin.action(description='Approve selected interests (clear needs_review)')
    def approve_interests(self, request, queryset):
        queryset.update(needs_review=False)
