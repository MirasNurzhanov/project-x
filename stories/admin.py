from django.contrib import admin
from .models import Story, StoryReaction


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user', 'moderation', 'place_name', 'likes_count',
        'created_at', 'expires_at',
    )
    # `moderation` leads, and is editable in place: "show me everything waiting
    # on a decision, and let me decide" is what this screen is opened for.
    list_filter = ('moderation', 'created_at')
    list_editable = ('moderation',)
    search_fields = ('user__username', 'place_name', 'content')


@admin.register(StoryReaction)
class StoryReactionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'story', 'reaction')
    list_filter = ('reaction',)
