from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'age_tier', 'location_sharing', 'is_private', 'date_joined')
    list_filter = ('age_tier', 'location_sharing', 'is_private', 'is_staff')
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
            'fields': (
                'show_on_leaderboard',
                'discoverable_by_username', 'discoverable_by_interest',
            ),
        }),
    )
