from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    ROLE_CHOICES = (
        ('superadmin', 'Super Admin'),
        ('admin', 'Admin'),
        ('staff', 'Staff'),
        ('client', 'Client'),
    )
    
    phone = models.CharField(max_length=15, unique=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='client')
    email_verified = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(null=True, blank=True)
    ghl_location_id = models.CharField(max_length=100, blank=True, null=True)
    ghl_contact_id = models.CharField(max_length=100, blank=True, null=True)
    is_paused = models.BooleanField(default=False, help_text="If True, user cannot login or access the system")
    date_of_birth = models.DateField(null=True, blank=True, help_text="Date of birth (optional)")
    
    def __str__(self):
        return f"{self.username} ({self.role})"

class StaffAvailability(models.Model):
    DAY_CHOICES = (
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    )
    
    staff = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'staff'})
    day_of_week = models.IntegerField(choices=DAY_CHOICES, null=True, blank=True)  # 0=Monday, 6=Sunday (nullable for migration)
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    class Meta:
        unique_together = ['staff', 'day_of_week', 'start_time']  # One availability entry per staff per day per start_time


class StaffDayAvailability(models.Model):
    """
    Day-specific availability for staff members (non-recurring).
    For example: Available on December 5, 2025 from 9 AM to 5 PM.
    """
    staff = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'staff'}, related_name='day_availabilities')
    date = models.DateField()  # Specific date (e.g., 2025-12-05)
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    class Meta:
        unique_together = ['staff', 'date', 'start_time']  # One availability entry per staff per date per start_time
        verbose_name_plural = 'Staff Day Availabilities'
        ordering = ['date', 'start_time']
    
    def __str__(self):
        return f"{self.staff.username} - {self.date} ({self.start_time} - {self.end_time})"