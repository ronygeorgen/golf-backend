from django.contrib import admin
from .models import SpecialEvent, SpecialEventRegistration, SpecialEventPausedDate, TempSpecialEventBooking

@admin.register(SpecialEvent)
class SpecialEventAdmin(admin.ModelAdmin):
    list_display = ('title', 'event_type', 'date', 'max_capacity', 'is_active', 'upfront_payment')
    list_filter = ('event_type', 'is_active', 'upfront_payment', 'date')
    search_fields = ('title', 'description')

@admin.register(SpecialEventRegistration)
class SpecialEventRegistrationAdmin(admin.ModelAdmin):
    list_display = ('event', 'user', 'occurrence_date', 'status', 'registered_at')
    list_filter = ('status', 'occurrence_date', 'event')
    search_fields = ('user__username', 'user__phone', 'event__title')

@admin.register(SpecialEventPausedDate)
class SpecialEventPausedDateAdmin(admin.ModelAdmin):
    list_display = ('event', 'date')
    list_filter = ('date', 'event')

@admin.register(TempSpecialEventBooking)
class TempSpecialEventBookingAdmin(admin.ModelAdmin):
    list_display = ('temp_id', 'event', 'user', 'occurrence_date', 'status', 'created_at', 'expires_at')
    list_filter = ('status', 'occurrence_date', 'event')
    search_fields = ('temp_id', 'user__username', 'user__phone', 'event__title')
