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
    day_of_week = models.IntegerField(choices=DAY_CHOICES, null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    # Phase: per-category availability — NULL means "applies to all categories" (general)
    service_category = models.ForeignKey(
        'categories.ServiceCategory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_availabilities',
        help_text="If set, this availability applies only to this service category. "
                  "Leave blank for general availability (applies to all categories).",
    )

    class Meta:
        # Composite uniqueness: one window per staff / day / start_time / category (NULL = general)
        unique_together = ['staff', 'day_of_week', 'start_time', 'service_category']


class StaffDayAvailability(models.Model):
    """
    Day-specific (non-recurring) availability for staff members.
    Example: Available on December 5, 2025 from 9 AM to 5 PM.
    Can optionally be scoped to a single service category.
    """
    staff = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'staff'}, related_name='day_availabilities')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    # Phase: per-category day availability
    service_category = models.ForeignKey(
        'categories.ServiceCategory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_day_availabilities',
        help_text="If set, this day-specific availability applies only to this service category. "
                  "Leave blank for general availability.",
    )

    class Meta:
        unique_together = ['staff', 'date', 'start_time', 'service_category']
        verbose_name_plural = 'Staff Day Availabilities'
        ordering = ['date', 'start_time']

    def __str__(self):
        cat = f" [{self.service_category.name}]" if self.service_category_id else ""
        return f"{self.staff.username} - {self.date} ({self.start_time} - {self.end_time}){cat}"


class StaffBlockedDate(models.Model):
    """
    Tracks specific dates/times when a staff member is blocked/unavailable.
    Supports both full-day and partial-day blocks.
    Can optionally be scoped to a single service category.

    Examples:
    - Full-day block (all categories): date=2026-02-16, start_time=None, end_time=None, service_category=None
    - Partial-day block (Fitness only): date=2026-02-16, start_time=10:00, end_time=15:00, service_category=<Fitness>
    """
    staff = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'staff'}, related_name='blocked_dates')
    date = models.DateField(help_text="Date when staff is blocked/unavailable")
    start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Start time of block (leave empty for full-day block). Wall-clock time in center's local timezone.",
    )
    end_time = models.TimeField(
        null=True,
        blank=True,
        help_text="End time of block (leave empty for full-day block). Wall-clock time in center's local timezone.",
    )
    reason = models.CharField(max_length=255, blank=True, null=True, help_text="Optional reason for blocking")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='staff_blocks_created', help_text="Admin who created this block")
    # Phase: per-category blocked dates
    service_category = models.ForeignKey(
        'categories.ServiceCategory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_blocked_dates',
        help_text="If set, this block applies only to this service category. "
                  "Leave blank to block across all categories.",
    )

    class Meta:
        unique_together = ['staff', 'date', 'start_time', 'end_time', 'service_category']
        verbose_name = 'Staff Blocked Date'
        verbose_name_plural = 'Staff Blocked Dates'
        ordering = ['date', 'start_time']
    
    def is_full_day_block(self):
        """Returns True if this is a full-day block (no specific times)"""
        return self.start_time is None and self.end_time is None
    
    def conflicts_with_time(self, check_start_time, check_end_time):
        """
        Check if a given time range conflicts with this block.
        
        Args:
            check_start_time: time object to check (start of slot)
            check_end_time: time object to check (end of slot)
            
        Returns:
            bool: True if there's a conflict, False otherwise
        """
        # Full-day block conflicts with everything
        if self.is_full_day_block():
            return True
        
        # Partial-day block: check for time overlap
        # Two time ranges overlap if: start1 < end2 AND start2 < end1
        return self.start_time < check_end_time and check_start_time < self.end_time
    
    def __str__(self):
        if self.is_full_day_block():
            return f"{self.staff.username} - Blocked on {self.date} (Full Day)"
        else:
            return f"{self.staff.username} - Blocked on {self.date} ({self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')})"


class StaffCategory(models.Model):
    """
    Phase D: links a staff member to the service categories they can deliver.
    Used for filtering available staff on the booking UI (future phases) and
    for display in the admin manage-staff page.
    """

    staff = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='staff_categories',
        limit_choices_to={'role__in': ['staff', 'admin']},
    )
    category = models.ForeignKey(
        'categories.ServiceCategory',
        on_delete=models.CASCADE,
        related_name='staff_assignments',
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Primary category for this staff member (used for display ordering).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['staff', 'category']
        ordering = ['-is_primary', 'category__sort_order', 'category__name']
        verbose_name = 'Staff Category'
        verbose_name_plural = 'Staff Categories'

    def __str__(self):
        return f"{self.staff.username} → {self.category.name}"


class LiabilityWaiverAcceptance(models.Model):
    """
    Model to track user acceptance of liability waivers.
    Stores acceptance timestamp in UTC.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='waiver_acceptances')
    waiver = models.ForeignKey('admin_panel.LiabilityWaiver', on_delete=models.CASCADE, related_name='acceptances')
    accepted_at = models.DateTimeField(
        help_text="Timestamp when user accepted the waiver (stored in UTC)"
    )
    waiver_content_hash = models.CharField(
        max_length=32,
        help_text="Hash of waiver content at time of acceptance to detect content changes"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'waiver']
        ordering = ['-accepted_at']
        verbose_name = 'Liability Waiver Acceptance'
        verbose_name_plural = 'Liability Waiver Acceptances'
    
    def __str__(self):
        return f"{self.user.username} accepted waiver on {self.accepted_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"