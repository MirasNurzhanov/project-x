from django.contrib import admin
from .models import Venue


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'address', 'is_active')
    list_filter = ('is_active', 'category')
    search_fields = ('name', 'address')
    prepopulated_fields = {'slug': ('name',)}
