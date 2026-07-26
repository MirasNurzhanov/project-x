from django.contrib import admin
from .models import Quest, QuestProof, UserAuraPoints, UserQuest

admin.site.register(Quest)
admin.site.register(UserQuest)
admin.site.register(UserAuraPoints)


@admin.register(QuestProof)
class QuestProofAdmin(admin.ModelAdmin):
    list_display = ('user', 'quest', 'status', 'submitted_at', 'exif_capture_time', 'reviewer')
    list_filter = ('status', 'quest__quest_type')
    readonly_fields = ('submitted_at',)
