from django.contrib import admin
from .models import ClosedDay


@admin.register(ClosedDay)
class ClosedDayAdmin(admin.ModelAdmin):
    list_display = ('title', 'start_date', 'end_date', 'start_time', 'end_time', 'recurrence', 'is_active', 'created_at')
    list_filter = ('recurrence', 'is_active', 'start_date')
    search_fields = ('title', 'description')
    date_hierarchy = 'start_date'
    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'description', 'is_active')
        }),
        ('Date & Time', {
            'fields': ('start_date', 'end_date', 'start_time', 'end_time')
        }),
        ('Recurrence', {
            'fields': ('recurrence',),
            'description': 'Select how often this closure repeats. One Time = only on the specified date(s). Weekly = every week on the same day. Yearly = every year on the same date.'
        }),
    )
    readonly_fields = ('created_at', 'updated_at')
