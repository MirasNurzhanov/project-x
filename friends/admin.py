from django.contrib import admin
from .models import BlockedUser, HiRequest, Message

admin.site.register(HiRequest)
admin.site.register(Message)
admin.site.register(BlockedUser)
