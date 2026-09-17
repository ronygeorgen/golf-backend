from django.conf import settings
from django.db import models
from django.utils import timezone

class Simulator(models.Model):
    name = models.CharField(max_length=100)
    bay_number = models.IntegerField()
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this simulator")
    is_active = models.BooleanField(default=True)
    is_coaching_bay = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    hourly_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Hourly rate (in USD) for normal simulator sessions."
    )
    redirect_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="URL to redirect to after simulator booking payment"
    )
    
    class Meta:
        unique_together = [['bay_number', 'location_id']]  # Bay number must be unique per location
    
    def __str__(self):
        return f"Bay {self.bay_number} - {self.name}"

class DurationPrice(models.Model):
    duration_minutes = models.IntegerField(unique=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    
    def __str__(self):
        return f"{self.duration_minutes}min - ${self.price}"

class SimulatorAvailability(models.Model):
    DAY_CHOICES = (
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    )
    
    simulator = models.ForeignKey(Simulator, on_delete=models.CASCADE, related_name='availabilities')
    day_of_week = models.IntegerField(choices=DAY_CHOICES)  # 0=Monday, 6=Sunday
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    class Meta:
        unique_together = ['simulator', 'day_of_week', 'start_time']  # One availability entry per simulator per day per start_time
        verbose_name_plural = 'Simulator Availabilities'
    
    def __str__(self):
        return f"{self.simulator.name} - {self.get_day_of_week_display()} ({self.start_time} - {self.end_time})"


class SimulatorBlockedDate(models.Model):
    """
    One-off blackout for a specific simulator/bay (full day or time range).
    Mirrors StaffBlockedDate so admins can block specific hours on a bay.
    """
    simulator = models.ForeignKey(Simulator, on_delete=models.CASCADE, related_name='blocked_dates')
    date = models.DateField(help_text="Date when this bay is blocked")
    start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Start time of block (empty = full-day). Wall-clock in center local timezone.",
    )
    end_time = models.TimeField(
        null=True,
        blank=True,
        help_text="End time of block (empty = full-day). Wall-clock in center local timezone.",
    )
    reason = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='simulator_blocks_created',
    )

    class Meta:
        unique_together = ['simulator', 'date', 'start_time', 'end_time']
        ordering = ['date', 'start_time']
        verbose_name = 'Simulator Blocked Date'
        verbose_name_plural = 'Simulator Blocked Dates'

    def is_full_day_block(self):
        return self.start_time is None and self.end_time is None

    def conflicts_with_time(self, check_start_time, check_end_time):
        if self.is_full_day_block():
            return True
        return self.start_time < check_end_time and check_start_time < self.end_time

    def __str__(self):
        if self.is_full_day_block():
            return f"{self.simulator.name} blocked {self.date} (Full Day)"
        return f"{self.simulator.name} blocked {self.date} ({self.start_time}-{self.end_time})"


class SimulatorCredit(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        REDEEMED = 'redeemed', 'Redeemed'
        REVOKED = 'revoked', 'Revoked'

    class Reason(models.TextChoices):
        CANCELLATION = 'cancellation', 'Cancellation Refund'
        MANUAL = 'manual', 'Manual Adjustment'

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='simulator_credits'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.CANCELLATION)
    hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text="Total hours in this credit"
    )
    hours_remaining = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text="Remaining hours available in this credit"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='issued_simulator_credits'
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    source_booking = models.ForeignKey(
        'bookings.Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generated_simulator_credits'
    )
    notes = models.CharField(max_length=255, blank=True)

    def mark_redeemed(self, booking):
        self.status = SimulatorCredit.Status.REDEEMED
        self.redeemed_at = timezone.now()
        self.save(update_fields=['status', 'redeemed_at'])
        if booking:
            booking.simulator_credit_redemption = self
            booking.save(update_fields=['simulator_credit_redemption'])
    
    def consume_hours(self, hours_to_consume):
        """
        Consume hours from this credit.
        
        Args:
            hours_to_consume: Decimal or float representing hours to consume
            
        Returns:
            bool: True if credit is fully consumed and should be marked as redeemed
        """
        from decimal import Decimal
        hours_to_consume = Decimal(str(hours_to_consume))
        if hours_to_consume <= 0:
            raise ValueError("hours_to_consume must be greater than 0")
        if self.hours_remaining < hours_to_consume:
            raise ValueError("Not enough hours remaining in this credit")
        
        self.hours_remaining -= hours_to_consume
        if self.hours_remaining <= 0:
            self.status = SimulatorCredit.Status.REDEEMED
            self.redeemed_at = timezone.now()
            self.save(update_fields=['hours_remaining', 'status', 'redeemed_at'])
            return True
        else:
            self.save(update_fields=['hours_remaining'])
            return False

    def __str__(self):
        return f"{self.client} - {self.get_status_display()} ({self.reason}) - {self.hours_remaining}/{self.hours} hrs"