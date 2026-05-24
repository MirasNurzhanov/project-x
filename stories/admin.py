from django.contrib import admin
from .models import Story, StoryReaction


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'place_name', 'likes_count', 'created_at', 'expires_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'place_name', 'content')


@admin.register(StoryReaction)
class StoryReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'story', 'reaction')
    list_filter = ('reaction',)
