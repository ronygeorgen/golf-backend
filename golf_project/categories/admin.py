from django.contrib import admin

from .models import ServiceCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'slug',
        'location_id',
        'legacy_booking_type',
        'sort_order',
        'is_active',
        'updated_at',
    )
    list_filter = ('is_active', 'legacy_booking_type')
    search_fields = ('name', 'slug', 'location_id', 'customer_label')
    ordering = ('location_id', 'sort_order', 'name')
