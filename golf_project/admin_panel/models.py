from django.db import models
from django.utils import timezone
from datetime import datetime, timedelta


class ClosedDay(models.Model):
    """
    Model to track closed/off days for the facility.
    Supports one-time, weekly recurring, and yearly recurring closures.
    Can block full days or specific time ranges.
    """
    RECURRENCE_CHOICES = (
        ('one_time', 'One Time (Specific Date Only)'),
        ('weekly', 'Weekly Recurring'),
        ('yearly', 'Yearly Recurring'),
    )
    
    title = models.CharField(max_length=200, help_text="Name/description of the closure (e.g., 'Holiday', 'Maintenance')")
    description = models.TextField(blank=True, help_text="Additional details about the closure")
    
    # Date range (for one-time closures, start_date = end_date)
    start_date = models.DateField(help_text="Start date of closure")
    end_date = models.DateField(help_text="End date of closure (same as start_date for single day)")
    
    # Time range (optional - if not set, entire day is closed)
    start_time = models.TimeField(
        null=True, 
        blank=True, 
        help_text="Start time of closure (leave empty for full day closure)"
    )
    end_time = models.TimeField(
        null=True, 
        blank=True, 
        help_text="End time of closure (leave empty for full day closure)"
    )
    
    recurrence = models.CharField(
        max_length=20, 
        choices=RECURRENCE_CHOICES, 
        default='one_time',
        help_text="How often this closure repeats"
    )
    
    is_active = models.BooleanField(default=True, help_text="Whether this closure is currently active")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['start_date', 'start_time']
        verbose_name = 'Closed Day'
        verbose_name_plural = 'Closed Days'
    
    def __str__(self):
        recurrence_str = self.get_recurrence_display()
        if self.start_time and self.end_time:
            return f"{self.title} - {self.start_date} ({self.start_time}-{self.end_time}) - {recurrence_str}"
        return f"{self.title} - {self.start_date} (Full Day) - {recurrence_str}"
    
    def clean(self):
        from django.core.exceptions import ValidationError
        if self.end_date < self.start_date:
            raise ValidationError("End date cannot be before start date.")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError("End time must be after start time.")
    
    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)
    
    def is_date_closed(self, check_date):
        """
        Check if a specific date is closed based on this closure rule.
        
        Args:
            check_date: datetime.date object to check
            
        Returns:
            bool: True if the date is closed, False otherwise
        """
        if not self.is_active:
            return False
        
        if self.recurrence == 'one_time':
            # One-time closure: check if date falls within the range
            return self.start_date <= check_date <= self.end_date
        
        elif self.recurrence == 'weekly':
            # Weekly recurring: check if the day of week matches
            # Get the day of week from start_date (0=Monday, 6=Sunday)
            start_day_of_week = self.start_date.weekday()
            check_day_of_week = check_date.weekday()
            return start_day_of_week == check_day_of_week
        
        elif self.recurrence == 'yearly':
            # Yearly recurring: check if month and day match
            return (self.start_date.month == check_date.month and 
                    self.start_date.day == check_date.day)
        
        return False
    
    def is_datetime_closed(self, check_datetime):
        """
        Check if a specific datetime is closed based on this closure rule.
        
        Args:
            check_datetime: datetime.datetime object to check
            
        Returns:
            tuple: (is_closed: bool, message: str)
        """
        if not self.is_active:
            return (False, None)
        
        check_date = check_datetime.date()
        check_time = check_datetime.time()
        
        # Check if date is closed
        if not self.is_date_closed(check_date):
            return (False, None)
        
        # If no time range specified, entire day is closed
        if not self.start_time or not self.end_time:
            return (True, f"{self.title}: Facility is closed on this day.")
        
        # Check if time falls within the closed time range
        if self.start_time <= check_time < self.end_time:
            return (True, f"{self.title}: Facility is closed from {self.start_time} to {self.end_time}.")
        
        return (False, None)
    
    @classmethod
    def check_if_closed(cls, check_datetime):
        """
        Check if a datetime is closed by any active closure rule.
        
        Args:
            check_datetime: datetime.datetime object to check
            
        Returns:
            tuple: (is_closed: bool, message: str or None)
        """
        active_closures = cls.objects.filter(is_active=True)
        
        for closure in active_closures:
            is_closed, message = closure.is_datetime_closed(check_datetime)
            if is_closed:
                return (True, message)
        
        return (False, None)
    
    @classmethod
    def check_if_date_closed(cls, check_date):
        """
        Check if a date is closed (any time during the day).
        
        Args:
            check_date: datetime.date object to check
            
        Returns:
            tuple: (is_closed: bool, closure_title: str or None)
        """
        active_closures = cls.objects.filter(is_active=True)
        
        for closure in active_closures:
            if closure.is_date_closed(check_date):
                # Check if it's a full day closure
                if not closure.start_time or not closure.end_time:
                    return (True, closure.title)
                # Even if it has time range, the date is considered closed
                return (True, closure.title)
        
        return (False, None)
