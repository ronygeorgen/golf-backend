from django.db import models

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
    
    # Common fields
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    total_price = models.DecimalField(max_digits=8, decimal_places=2)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.client.username} - {self.booking_type} - {self.start_time}"