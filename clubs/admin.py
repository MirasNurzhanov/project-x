from django.contrib import admin
from .models import Club, ClubMember


class ClubMemberInline(admin.TabularInline):
    model = ClubMember
    extra = 0
    readonly_fields = ('joined_at',)


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = ('name', 'interest', 'is_active', 'created_at')
    list_filter = ('is_active', 'interest')
    search_fields = ('name',)
    inlines = [ClubMemberInline]


@admin.register(ClubMember)
class ClubMemberAdmin(admin.ModelAdmin):
    list_display = ('user', 'club', 'role', 'joined_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'club__name')
