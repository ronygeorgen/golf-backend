from django.conf import settings
from django.db import models


class CoachingPackage(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    staff_members = models.ManyToManyField('users.User', limit_choices_to={'role': 'staff'})
    session_count = models.PositiveIntegerField(default=1, help_text="How many coaching sessions are included.")
    session_duration_minutes = models.PositiveIntegerField(default=60, help_text="Duration of a single session in minutes.")
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.title


class CoachingPackagePurchase(models.Model):
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='coaching_purchases'
    )
    package = models.ForeignKey(
        CoachingPackage,
        on_delete=models.CASCADE,
        related_name='purchases'
    )
    sessions_total = models.PositiveIntegerField()
    sessions_remaining = models.PositiveIntegerField()
    notes = models.CharField(max_length=255, blank=True)
    purchased_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-purchased_at']
        verbose_name = 'Coaching Package Purchase'
        verbose_name_plural = 'Coaching Package Purchases'
    
    def __str__(self):
        return f"{self.client.username} - {self.package.title} ({self.sessions_remaining}/{self.sessions_total})"
    
    @property
    def is_depleted(self):
        return self.sessions_remaining <= 0
    
    def consume_session(self, count=1):
        if count < 1:
            raise ValueError("count must be at least 1")
        if self.sessions_remaining < count:
            raise ValueError("Not enough sessions remaining")
        self.sessions_remaining -= count
        self.save(update_fields=['sessions_remaining', 'updated_at'])