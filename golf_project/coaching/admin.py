from django.contrib import admin
from .models import (
    CoachingPackage,
    CoachingPackagePurchase,
    SimulatorPackage,
    SimulatorPackagePurchase,
)


@admin.register(CoachingPackage)
class CoachingPackageAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'price', 'session_count', 'simulator_hours', 'category_hours', 'service_category', 'is_active', 'location_id')
    list_filter = ('is_active', 'service_category')
    search_fields = ('title',)


@admin.register(CoachingPackagePurchase)
class CoachingPackagePurchaseAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'client', 'package', 'sessions_remaining', 'sessions_total',
        'simulator_hours_remaining', 'category_hours_remaining',
        'package_status', 'purchase_type', 'purchased_at',
    )
    list_filter = ('package_status', 'purchase_type')
    search_fields = ('client__phone', 'client__first_name', 'package__title')
    raw_id_fields = ('client', 'package')
    readonly_fields = ('purchased_at',)


@admin.register(SimulatorPackage)
class SimulatorPackageAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'price', 'hours', 'is_active', 'location_id')
    list_filter = ('is_active',)
    search_fields = ('title',)


@admin.register(SimulatorPackagePurchase)
class SimulatorPackagePurchaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'package', 'hours_remaining', 'package_status', 'purchased_at')
    list_filter = ('package_status',)
    search_fields = ('client__phone', 'client__first_name', 'package__title')
    raw_id_fields = ('client', 'package')
    readonly_fields = ('purchased_at',)


