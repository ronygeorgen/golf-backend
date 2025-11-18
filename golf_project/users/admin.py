from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, StaffAvailability

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('username', 'email', 'phone', 'role', 'is_staff', 'is_superuser', 'is_active')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active')
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Additional Info', {'fields': ('phone', 'role', 'email_verified', 'phone_verified')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Additional Info', {'fields': ('phone', 'role', 'email')}),
    )
    
    def save_model(self, request, obj, form, change):
        # Automatically set role to 'admin' for superusers
        if obj.is_superuser and not obj.role:
            obj.role = 'admin'
        super().save_model(request, obj, form, change)

@admin.register(StaffAvailability)
class StaffAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('staff', 'day_of_week', 'start_time', 'end_time')
    list_filter = ('day_of_week', 'staff')
