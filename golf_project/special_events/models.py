from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import datetime, timedelta


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
    
    def get_available_spots(self, occurrence_date=None):
        """Get remaining available spots for a specific occurrence date"""
        registered = self.get_registered_count(occurrence_date)
        return max(0, self.max_capacity - registered)
    
    def is_full(self, occurrence_date=None):
        """Check if event is at capacity for a specific occurrence date"""
        return self.get_registered_count(occurrence_date) >= self.max_capacity
    
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
