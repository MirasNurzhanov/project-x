from django.contrib import admin

from .models import Interest


@admin.register(Interest)
class InterestAdmin(admin.ModelAdmin):
    # `category` is editable in the list because the one thing that needs doing
    # by hand is filing a custom interest under a heading — the seeder handles
    # everything the catalogue knows about, and will not touch a row it has
    # never heard of.
    list_display = ('name', 'icon', 'category', 'needs_review')
    list_filter = ('category', 'needs_review')
    list_editable = ('category',)
    search_fields = ('name',)
    actions = ('approve_interests',)

    @admin.action(description='Approve selected interests (clear needs_review)')
    def approve_interests(self, request, queryset):
        queryset.update(needs_review=False)
