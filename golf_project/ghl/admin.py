from django.contrib import admin
from .models import GHLLocation


@admin.register(GHLLocation)
class GHLLocationAdmin(admin.ModelAdmin):
    list_display = ('location_id', 'company_name', 'status', 'onboarded_at', 'created_at')
    search_fields = ('location_id', 'company_name')
    readonly_fields = ('created_at', 'updated_at', 'onboarded_at')
    ordering = ('-created_at',)


