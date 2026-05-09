from django.contrib import admin
from .models import Quest, UserAuraPoints, UserQuest

admin.site.register(Quest)
admin.site.register(UserQuest)
admin.site.register(UserAuraPoints)
