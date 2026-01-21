from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import datetime, timedelta
import uuid


class SpecialEvent(models.Model):
    EVENT_TYPE_CHOICES = (
        ('one_time', 'One Time'),
        ('weekly', 'Weekly Recurring'),
        ('monthly', 'Monthly Recurring'),
        ('yearly', 'Yearly Recurring'),
    )
    
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this event")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES, default='one_time')
    
    # For one-time events: use date and time directly
    # For recurring events: use date as the start date, and calculate occurrences
    date = models.DateField()  # Start date for recurring events
    recurring_end_date = models.DateField(
        null=True, 
        blank=True,
        help_text="End date for recurring events. Recurring occurrences will stop on this date."
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    max_capacity = models.PositiveIntegerField(help_text="Maximum number of users that can register")
    is_active = models.BooleanField(default=True)
    
    price = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Optional price for the event"
    )
    show_price = models.BooleanField(
        default=False,
        help_text="Toggle to show/hide price on user-facing page"
    )
    is_private = models.BooleanField(
        default=False,
        help_text="If True, this event is private and only visible to admins. Clients cannot see or register for private events."
    )
    is_auto_enroll = models.BooleanField(
        default=False,
        help_text="If True, registered customers will be automatically enrolled for the next occurrence. Only applicable for weekly and monthly recurring events."
    )
    upfront_payment = models.BooleanField(
        default=False,
        help_text="If True, users must pay upfront to register. This creates a temporary hold on the spot until payment is confirmed."
    )
    redirect_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="URL to redirect to after event registration payment (if upfront_payment is True)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['date', 'start_time']
        verbose_name = 'Special Event'
        verbose_name_plural = 'Special Events'
    
    def __str__(self):
        return f"{self.title} - {self.date} ({self.start_time} - {self.end_time})"
    
    def get_registered_count(self, occurrence_date=None):
        """Get the number of registered users for a specific occurrence date (counts both registered and showed_up)"""
        if occurrence_date is None:
            # If no date specified, count all (for backward compatibility)
            return self.registrations.filter(status__in=['registered', 'showed_up']).count()
        return self.registrations.filter(
            occurrence_date=occurrence_date,
            status__in=['registered', 'showed_up']
        ).count()
    
    def get_showed_up_count(self, occurrence_date=None):
        """Get the number of users who showed up for a specific occurrence date"""
        if occurrence_date is None:
            return self.registrations.filter(status='showed_up').count()
        return self.registrations.filter(
            occurrence_date=occurrence_date,
            status='showed_up'
        ).count()
    
    def get_temp_reserved_count(self, occurrence_date=None):
        """Get the number of spots currently held by active temp bookings"""
        if occurrence_date is None:
            return 0
        
        # Use the related manager
        return self.temp_bookings.filter(
            occurrence_date=occurrence_date,
            status='reserved',
            expires_at__gt=timezone.now()
        ).count()

    def get_available_spots(self, occurrence_date=None):
        """Get remaining available spots for a specific occurrence date"""
        registered = self.get_registered_count(occurrence_date)
        temp_reserved = self.get_temp_reserved_count(occurrence_date)
        return max(0, self.max_capacity - (registered + temp_reserved))
    
    def is_full(self, occurrence_date=None):
        """Check if event is at capacity for a specific occurrence date"""
        return self.get_available_spots(occurrence_date) <= 0
    
    def get_occurrences(self, start_date=None, end_date=None):
        """
        Get all occurrences of this event within a date range.
        For one-time events, returns just the single date if it's in range.
        For recurring events, calculates all occurrences up to recurring_end_date if set.
        """
        if start_date is None:
            start_date = timezone.now().date()
        if end_date is None:
            end_date = start_date + timedelta(days=365)  # Default to 1 year ahead
        
        occurrences = []
        
        # For recurring events, use recurring_end_date as the limit if set
        recurring_limit = self.recurring_end_date if self.recurring_end_date else end_date
        # Use the earlier of recurring_limit or end_date
        effective_end_date = min(recurring_limit, end_date) if recurring_limit else end_date
        
        if self.event_type == 'one_time':
            if start_date <= self.date <= end_date:
                occurrences.append(self.date)
        elif self.event_type == 'weekly':
            current_date = self.date
            while current_date <= effective_end_date:
                if current_date >= start_date:
                    occurrences.append(current_date)
                current_date += timedelta(weeks=1)
        elif self.event_type == 'monthly':
            current_date = self.date
            while current_date <= effective_end_date:
                if current_date >= start_date:
                    occurrences.append(current_date)
                # Move to next month (handle month-end edge cases)
                if current_date.month == 12:
                    current_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    current_date = current_date.replace(month=current_date.month + 1)
        elif self.event_type == 'yearly':
            current_date = self.date
            while current_date <= effective_end_date:
                if current_date >= start_date:
                    occurrences.append(current_date)
                current_date = current_date.replace(year=current_date.year + 1)
        

        
        # Filter out paused dates
        paused_dates = set(self.paused_dates.values_list('date', flat=True))
        occurrences = [d for d in occurrences if d not in paused_dates]
        
        return occurrences
    
    def conflicts_with_datetime(self, check_datetime):
        """
        Check if a given datetime conflicts with this event.
        Returns True if the datetime falls within any occurrence of this event.
        """
        check_date = check_datetime.date()
        check_time = check_datetime.time()
        
        occurrences = self.get_occurrences(
            start_date=check_date - timedelta(days=1),
            end_date=check_date + timedelta(days=1)
        )
        
        if check_date not in occurrences:
            return False
        
        # Check if time overlaps
        event_start = self.start_time
        event_end = self.end_time
        
        # Handle case where end_time is before start_time (crosses midnight)
        if event_end < event_start:
            # Event crosses midnight
            return check_time >= event_start or check_time <= event_end
        else:
            # Normal case
            return event_start <= check_time < event_end
    
    def auto_enroll_users_for_next_occurrence(self):
        """
        Auto-enroll users who were registered for the previous occurrence.
        Only works for weekly and monthly recurring events with is_auto_enroll=True.
        """
        # Only for weekly and monthly recurring events
        if self.event_type not in ['weekly', 'monthly']:
            return
        
        # Only if auto-enroll is enabled
        if not self.is_auto_enroll:
            return
        
        today = timezone.now().date()
        
        # Get all occurrences
        occurrences = self.get_occurrences(
            start_date=self.date,
            end_date=today + timedelta(days=365)
        )
        
        if len(occurrences) < 2:
            # Need at least 2 occurrences (previous and next)
            return
        
        # Find the most recent past occurrence and the next upcoming occurrence
        previous_occurrence = None
        next_occurrence = None
        
        # Find the most recent past occurrence
        for occurrence in reversed(occurrences):
            if occurrence < today:
                previous_occurrence = occurrence
                break
        
        # Find the next upcoming occurrence
        for occurrence in occurrences:
            if occurrence > today:
                next_occurrence = occurrence
                break
        
        if not previous_occurrence or not next_occurrence:
            return
        
        # Get all users who were registered (not cancelled) for the previous occurrence
        previous_registrations = self.registrations.filter(
            occurrence_date=previous_occurrence,
            status__in=['registered', 'showed_up']
        )
        
        # Auto-enroll them for the next occurrence
        for registration in previous_registrations:
            # Check if already registered for next occurrence
            existing_registration = self.registrations.filter(
                user=registration.user,
                occurrence_date=next_occurrence
            ).first()
            
            if not existing_registration:
                # Check if event is full for next occurrence
                next_registered_count = self.get_registered_count(next_occurrence)
                if next_registered_count < self.max_capacity:
                    # Create auto-enrollment
                    SpecialEventRegistration.objects.create(
                        event=self,
                        user=registration.user,
                        occurrence_date=next_occurrence,
                        status='registered'
                    )


class SpecialEventPausedDate(models.Model):
    event = models.ForeignKey(
        SpecialEvent,
        on_delete=models.CASCADE,
        related_name='paused_dates'
    )
    date = models.DateField(help_text="The date of the occurrence to pause")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = [['event', 'date']]
        verbose_name = 'Paused Event Date'
        ordering = ['date']
        
    def __str__(self):
        return f"{self.event.title} - Paused: {self.date}"


class TempSpecialEventBooking(models.Model):
    """
    Temporary booking record created before payment processing for special events.
    Stores booking details until webhook confirms payment.
    """
    STATUS_CHOICES = (
        ('reserved', 'Reserved'),
        ('completed', 'Completed'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    )
    
    temp_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        SpecialEvent,
        on_delete=models.CASCADE,
        related_name='temp_bookings'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='temp_event_bookings'
    )
    occurrence_date = models.DateField(help_text="The specific date of the event occurrence")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='reserved'
    )
    payment_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        help_text="External payment ID"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Expiration time for temp booking (default 9 mins)"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Temporary Event Booking'
        verbose_name_plural = 'Temporary Event Bookings'
    
    def __str__(self):
        return f"TempEventBooking {self.temp_id} - {self.user.username} - {self.event.title}"
    
    def save(self, *args, **kwargs):
        if not self.expires_at:
            # User has 9 minutes to complete payment before slot is released
            self.expires_at = timezone.now() + timedelta(minutes=9)
        super().save(*args, **kwargs)
    
    @property
    def is_expired(self):
        if self.expires_at:
            return timezone.now() > self.expires_at
        return False
        
    @property
    def is_active(self):
        return self.status == 'reserved' and not self.is_expired



class SpecialEventRegistration(models.Model):
    STATUS_CHOICES = (
        ('registered', 'Registered'),
        ('showed_up', 'Showed Up'),
        ('cancelled', 'Cancelled'),
    )
    
    event = models.ForeignKey(
        SpecialEvent,
        on_delete=models.CASCADE,
        related_name='registrations'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='event_registrations'
    )
    occurrence_date = models.DateField(help_text="The specific date of the event occurrence this registration is for")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='registered')
    registered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = [['event', 'user', 'occurrence_date']]  # One registration per user per event occurrence
        ordering = ['-registered_at']
        verbose_name = 'Event Registration'
        verbose_name_plural = 'Event Registrations'
    
    def __str__(self):
        return f"{self.user.username} - {self.event.title} ({self.occurrence_date}) ({self.status})"
