from django.db import models
import uuid
from django.utils import timezone
from datetime import timedelta

class Booking(models.Model):
    BOOKING_TYPE_CHOICES = (
        ('simulator', 'Simulator'),
        ('coaching', 'Coaching'),
    )
    
    STATUS_CHOICES = (
        ('confirmed', 'Confirmed'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    )
    
    client = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='bookings')
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this booking")
    booking_type = models.CharField(max_length=10, choices=BOOKING_TYPE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='confirmed')
    
    # Simulator booking fields
    simulator = models.ForeignKey('simulators.Simulator', on_delete=models.CASCADE, null=True, blank=True)
    duration_minutes = models.IntegerField(null=True, blank=True)
    
    # Coaching booking fields
    coaching_package = models.ForeignKey('coaching.CoachingPackage', on_delete=models.CASCADE, null=True, blank=True)
    coach = models.ForeignKey('users.User', on_delete=models.CASCADE, null=True, blank=True, related_name='coaching_sessions')
    package_purchase = models.ForeignKey(
        'coaching.CoachingPackagePurchase',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bookings'
    )
    simulator_package_purchase = models.ForeignKey(
        'coaching.SimulatorPackagePurchase',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bookings'
    )
    
    # Common fields
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    total_price = models.DecimalField(max_digits=8, decimal_places=2)
    simulator_credit_redemption = models.OneToOneField(
        'simulators.SimulatorCredit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='redeemed_booking'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.client.username} - {self.booking_type} - {self.start_time}"


class TempBooking(models.Model):
    """
    Temporary booking record created before payment processing for simulator bookings.
    Stores booking details until webhook confirms payment.
    """
    temp_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    simulator = models.ForeignKey(
        'simulators.Simulator',
        on_delete=models.CASCADE,
        related_name='temp_bookings'
    )
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this temp booking")
    buyer_phone = models.CharField(max_length=15)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    duration_minutes = models.IntegerField(help_text="Duration per simulator in minutes")
    simulator_count = models.IntegerField(default=1, help_text="Number of simulators to book")
    total_price = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Expiration time for temp booking (default 24 hours)"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Temporary Booking'
        verbose_name_plural = 'Temporary Bookings'
    
    def __str__(self):
        return f"TempBooking {self.temp_id} - {self.buyer_phone} - {self.start_time}"
    
    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)
    
    @property
    def is_expired(self):
        if self.expires_at:
            return timezone.now() > self.expires_at
        return False