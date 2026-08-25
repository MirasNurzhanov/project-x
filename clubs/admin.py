from django.contrib import admin
from .models import Club, ClubMember, ClubMessage


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


@admin.register(ClubMessage)
class ClubMessageAdmin(admin.ModelAdmin):
    """Club chat, for moderation only.

    A club is semi-public - joining hands you the whole history - so its
    messages take the same screening and the same takedown route story text
    does. `moderation` is editable in place for the same reason it is on
    stories.
    """

    list_display = ('id', 'club', 'sender', 'moderation', 'preview', 'created_at')
    list_filter = ('moderation', 'message_type', 'created_at')
    list_editable = ('moderation',)
    search_fields = ('sender__username', 'content', 'club__name')
    list_select_related = ('club', 'sender')
    readonly_fields = ('club', 'sender', 'content', 'image', 'image_url', 'created_at')

    @admin.display(description='Message')
    def preview(self, obj):
        text = ' '.join((obj.content or '').split())
        return (text[:79] + '…') if len(text) > 80 else (text or '[image]')

    def has_add_permission(self, request):
        return False
