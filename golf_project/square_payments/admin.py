from django.contrib import admin
from .models import LocationSquareAccount


@admin.register(LocationSquareAccount)
class LocationSquareAccountAdmin(admin.ModelAdmin):
    list_display = [
        'ghl_location', 'merchant_id', 'square_location_id',
        'square_location_name', 'is_connected', 'token_valid_display',
        'connected_at',
    ]
    list_filter = ['is_connected']
    search_fields = [
        'merchant_id', 'square_location_id',
        'ghl_location__location_id', 'ghl_location__company_name',
    ]
    readonly_fields = [
        'merchant_id', 'square_location_id', 'square_location_name',
        'token_expires_at', 'connected_at', 'created_at', 'updated_at',
    ]
    fieldsets = (
        ('GHL Location', {
            'fields': ('ghl_location',),
        }),
        ('Square Account', {
            'fields': ('merchant_id', 'square_location_id', 'square_location_name', 'is_connected', 'connected_at'),
        }),
        ('OAuth Tokens', {
            'classes': ('collapse',),
            'fields': ('access_token', 'refresh_token', 'token_expires_at'),
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Token Valid', boolean=True)
    def token_valid_display(self, obj):
        return obj.is_token_valid()
