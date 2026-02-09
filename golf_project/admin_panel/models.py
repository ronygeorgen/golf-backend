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
    
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this closed day")
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
        if self.start_time and self.end_time:
            # Combine date and time to handle midnight crossovers
            start_dt = datetime.combine(self.start_date, self.start_time)
            end_dt = datetime.combine(self.end_date, self.end_time)
            if end_dt <= start_dt:
                raise ValidationError("End date and time must be after start date and time.")
    
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
            # Weekly recurring: check if the day of week falls within the range
            # Get the day of week from start and end dates (0=Monday, 6=Sunday)
            start_dow = self.start_date.weekday()
            end_dow = self.end_date.weekday()
            check_dow = check_date.weekday()
            
            if start_dow <= end_dow:
                # Normal range within a week (e.g., Mon-Wed)
                return start_dow <= check_dow <= end_dow
            else:
                # Spans across Sunday-Monday boundary (e.g., Sat-Tue)
                return check_dow >= start_dow or check_dow <= end_dow
        
        elif self.recurrence == 'yearly':
            # Yearly recurring: check if month and day match a date in the range
            # For simplicity, we compare (month, day) tuples
            start_md = (self.start_date.month, self.start_date.day)
            end_md = (self.end_date.month, self.end_date.day)
            check_md = (check_date.month, check_date.day)
            
            if start_md <= end_md:
                return start_md <= check_md <= end_md
            else:
                # Spans across New Year (e.g., Dec 30 - Jan 2)
                return check_md >= start_md or check_md <= end_md
        
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
        
        # For one_time closures, we can check the exact range with datetimes
        if self.recurrence == 'one_time':
            start_dt = datetime.combine(self.start_date, self.start_time)
            end_dt = datetime.combine(self.end_date, self.end_time)
            
            # Ensure both are either naive or aware to allow comparison
            if check_datetime.tzinfo:
                start_dt = start_dt.replace(tzinfo=check_datetime.tzinfo)
                end_dt = end_dt.replace(tzinfo=check_datetime.tzinfo)
                
            if start_dt <= check_datetime < end_dt:
                return (True, f"{self.title}: Facility is closed from {self.start_time.strftime('%H:%M')} to {self.end_time.strftime('%H:%M')}.")
            return (False, None)
            
        # For recurring closures, check if time falls within the range
        # If it's a multi-day closure, the check depends on which part of the range we're on
        
        # Normalized day of week for range checks
        start_dow = self.start_date.weekday()
        end_dow = self.end_date.weekday()
        check_dow = check_date.weekday()
        
        is_multi_day = self.start_date != self.end_date
        
        if not is_multi_day:
            # Single day range: start_time <= check_time < end_time
            # (Note: we already know check_date matches)
            if self.start_time <= check_time < self.end_time:
                return (True, f"{self.title}: Facility is closed from {self.start_time.strftime('%H:%M')} to {self.end_time.strftime('%H:%M')}.")
        else:
            # Multi-day range
            # 1. If it's the start date: check check_time >= start_time
            # 2. If it's the end date: check check_time < end_time
            # 3. If it's a day in between: it's closed regardless of time
            
            # Use recurrence-specific matching for start/end days
            if self.recurrence == 'weekly':
                if check_dow == start_dow:
                    if check_time >= self.start_time:
                        return (True, f"{self.title}: Facility is closed (starts {self.start_time.strftime('%H:%M')}).")
                    return (False, None)
                elif check_dow == end_dow:
                    if check_time < self.end_time:
                        return (True, f"{self.title}: Facility is closed (until {self.end_time.strftime('%H:%M')}).")
                    return (False, None)
                else:
                    # middle day
                    return (True, f"{self.title}: Facility is closed.")
            
            elif self.recurrence == 'yearly':
                check_md = (check_date.month, check_date.day)
                start_md = (self.start_date.month, self.start_date.day)
                end_md = (self.end_date.month, self.end_date.day)
                
                if check_md == start_md:
                    if check_time >= self.start_time:
                        return (True, f"{self.title}: Facility is closed (starts {self.start_time.strftime('%H:%M')}).")
                    return (False, None)
                elif check_md == end_md:
                    if check_time < self.end_time:
                        return (True, f"{self.title}: Facility is closed (until {self.end_time.strftime('%H:%M')}).")
                    return (False, None)
                else:
                    # middle day
                    return (True, f"{self.title}: Facility is closed.")
        
        return (False, None)
    
    @classmethod
    def check_if_closed(cls, check_datetime, location_id=None):
        """
        Check if a datetime is closed by any active closure rule.
        
        Args:
            check_datetime: datetime.datetime object to check
            location_id: Optional location_id to filter closures
            
        Returns:
            tuple: (is_closed: bool, message: str or None)
        """
        active_closures = cls.objects.filter(is_active=True)
        if location_id:
            active_closures = active_closures.filter(location_id=location_id)
        
        for closure in active_closures:
            is_closed, message = closure.is_datetime_closed(check_datetime)
            if is_closed:
                return (True, message)
        
        return (False, None)
    
    @classmethod
    def check_if_date_closed(cls, check_date, location_id=None):
        """
        Check if a date is closed (any time during the day).
        
        Args:
            check_date: datetime.date object to check
            location_id: Optional location_id to filter closures
            
        Returns:
            tuple: (is_closed: bool, closure_title: str or None)
        """
        active_closures = cls.objects.filter(is_active=True)
        if location_id:
            active_closures = active_closures.filter(location_id=location_id)
        
        for closure in active_closures:
            if closure.is_date_closed(check_date):
                # Check if it's a full day closure
                if not closure.start_time or not closure.end_time:
                    return (True, closure.title)
                # Even if it has time range, the date is considered closed
                return (True, closure.title)
        
        return (False, None)


class LiabilityWaiver(models.Model):
    """
    Model to store liability waiver content.
    Only one active waiver can exist at a time.
    Supports rich text formatting (heading, bold, italic, new lines).
    """
    content = models.JSONField(
        help_text="Rich text content as JSON array. Each item has 'type' (heading/paragraph), 'text', and optional 'bold', 'italic'"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="If False, waiver will not be shown to users during login"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Liability Waiver'
        verbose_name_plural = 'Liability Waivers'
    
    def __str__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"Liability Waiver ({status}) - {self.created_at.strftime('%Y-%m-%d')}"
    
    def save(self, *args, **kwargs):
        # Ensure only one active waiver exists
        if self.is_active:
            # Deactivate all other waivers
            LiabilityWaiver.objects.filter(is_active=True).exclude(pk=self.pk if self.pk else None).update(is_active=False)
        super().save(*args, **kwargs)
    
    def get_content_hash(self):
        """Generate a hash of the content to detect changes"""
        import hashlib
        import json
        content_str = json.dumps(self.content, sort_keys=True)
        return hashlib.md5(content_str.encode()).hexdigest()