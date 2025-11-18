from django.contrib import admin
from .models import Simulator, DurationPrice, SimulatorAvailability

@admin.register(Simulator)
class SimulatorAdmin(admin.ModelAdmin):
    list_display = ('name', 'bay_number', 'is_active', 'is_coaching_bay')
    list_filter = ('is_active', 'is_coaching_bay')

@admin.register(DurationPrice)
class DurationPriceAdmin(admin.ModelAdmin):
    list_display = ('duration_minutes', 'price')

@admin.register(SimulatorAvailability)
class SimulatorAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('simulator', 'day_of_week', 'start_time', 'end_time')
    list_filter = ('day_of_week', 'simulator')
