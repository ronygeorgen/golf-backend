from django.conf import settings
from django.db import models
from django.utils import timezone

class Simulator(models.Model):
    name = models.CharField(max_length=100)
    bay_number = models.IntegerField(unique=True)
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

    def __str__(self):
        return f"{self.client} - {self.get_status_display()} ({self.reason})"