from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'age_tier', 'privacy_mode', 'date_joined')
    list_filter = ('age_tier', 'privacy_mode', 'is_staff')
    fieldsets = UserAdmin.fieldsets + (
        ('Profile', {'fields': ('bio', 'date_of_birth', 'age_tier', 'profile_picture')}),
        ('Privacy', {'fields': ('privacy_mode',)}),
    )