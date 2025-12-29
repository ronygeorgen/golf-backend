from django.conf import settings
from django.db import models
import secrets
import string
import uuid


class CoachingPackage(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this package")
    staff_members = models.ManyToManyField('users.User', limit_choices_to={'role': 'staff'})
    session_count = models.PositiveIntegerField(default=1, help_text="How many coaching sessions are included.")
    session_duration_minutes = models.PositiveIntegerField(default=60, help_text="Duration of a single session in minutes.")
    simulator_hours = models.DecimalField(
        max_digits=6, 
        decimal_places=2, 
        default=0, 
        help_text="Number of simulator hours included in this package (for simulator bookings)."
    )
    redirect_url = models.URLField(max_length=500, blank=True, null=True, help_text="URL to redirect to after package purchase")
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.title


class CoachingPackagePurchase(models.Model):
    PURCHASE_TYPE_CHOICES = (
        ('normal', 'Normal'),
        ('gift', 'Gift'),
        ('organization', 'Organization'),
    )
    
    GIFT_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    )
    
    PACKAGE_STATUS_CHOICES = (
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('gifted', 'Gifted'),
    )
    
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
    purchase_name = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Custom label for this purchase"
    )
    sessions_total = models.PositiveIntegerField()
    sessions_remaining = models.PositiveIntegerField()
    simulator_hours_total = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text="Total simulator hours included in this purchase"
    )
    simulator_hours_remaining = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text="Remaining simulator hours in this purchase"
    )
    notes = models.CharField(max_length=255, blank=True)
    
    # Gifting fields
    purchase_type = models.CharField(max_length=15, choices=PURCHASE_TYPE_CHOICES, default='normal')
    recipient_phone = models.CharField(max_length=15, blank=True, null=True, help_text="Phone number of gift recipient")
    gift_status = models.CharField(max_length=10, choices=GIFT_STATUS_CHOICES, blank=True, null=True)
    gift_token = models.CharField(max_length=64, unique=True, blank=True, null=True, help_text="Unique token for gift claim")
    original_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='gifted_packages',
        help_text="Original purchaser for gifted packages"
    )
    package_status = models.CharField(max_length=10, choices=PACKAGE_STATUS_CHOICES, default='active')
    
    purchased_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    gift_expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiration date for gift claim")
    referral_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='referred_purchases',
        help_text="Staff member who referred this purchase (optional)"
    )
    
    class Meta:
        ordering = ['-purchased_at']
        verbose_name = 'Coaching Package Purchase'
        verbose_name_plural = 'Coaching Package Purchases'
    
    def __str__(self):
        hours_str = f", {self.simulator_hours_remaining}/{self.simulator_hours_total} hrs" if self.simulator_hours_total > 0 else ""
        return f"{self.purchase_name} - {self.package.title} ({self.sessions_remaining}/{self.sessions_total} sessions{hours_str})"
    
    def generate_gift_token(self):
        """Generate a unique gift claim token"""
        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))
        # Ensure uniqueness
        while CoachingPackagePurchase.objects.filter(gift_token=token).exists():
            token = ''.join(secrets.choice(alphabet) for _ in range(32))
        return token
    
    @property
    def is_depleted(self):
        return self.sessions_remaining <= 0 and self.simulator_hours_remaining <= 0
    
    @property
    def is_gift_pending(self):
        return self.purchase_type == 'gift' and self.gift_status == 'pending'
    
    @property
    def can_be_transferred(self):
        """Check if package can have sessions transferred"""
        return (
            self.package_status == 'active' and
            self.sessions_remaining > 0 and
            not self.is_gift_pending
        )
    
    def consume_session(self, count=1):
        if count < 1:
            raise ValueError("count must be at least 1")
        if self.sessions_remaining < count:
            raise ValueError("Not enough sessions remaining")
        self.sessions_remaining -= count
        if self.sessions_remaining == 0 and self.simulator_hours_remaining <= 0:
            self.package_status = 'completed'
        self.save(update_fields=['sessions_remaining', 'package_status', 'updated_at'])
    
    def consume_simulator_hours(self, hours):
        """
        Consume simulator hours from this purchase.
        
        Args:
            hours: Decimal or float representing hours to consume
        """
        from decimal import Decimal
        hours = Decimal(str(hours))
        if hours <= 0:
            raise ValueError("hours must be greater than 0")
        if self.simulator_hours_remaining < hours:
            raise ValueError("Not enough simulator hours remaining")
        self.simulator_hours_remaining -= hours
        if self.sessions_remaining == 0 and self.simulator_hours_remaining <= 0:
            self.package_status = 'completed'
        self.save(update_fields=['simulator_hours_remaining', 'package_status', 'updated_at'])


class SessionTransfer(models.Model):
    TRANSFER_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    )
    
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_transfers'
    )
    to_user_phone = models.CharField(max_length=15, help_text="Phone number of transfer recipient")
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_transfers',
        help_text="Set when recipient accepts transfer"
    )
    package_purchase = models.ForeignKey(
        CoachingPackagePurchase,
        on_delete=models.CASCADE,
        related_name='transfers'
    )
    session_count = models.PositiveIntegerField(help_text="Number of sessions to transfer")
    transfer_status = models.CharField(max_length=10, choices=TRANSFER_STATUS_CHOICES, default='pending')
    transfer_token = models.CharField(max_length=64, unique=True, help_text="Unique token for transfer claim")
    notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiration date for transfer claim")
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Session Transfer'
        verbose_name_plural = 'Session Transfers'
    
    def __str__(self):
        return f"{self.from_user.username} → {self.to_user_phone} ({self.session_count} sessions)"
    
    def generate_transfer_token(self):
        """Generate a unique transfer claim token"""
        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))
        # Ensure uniqueness
        while SessionTransfer.objects.filter(transfer_token=token).exists():
            token = ''.join(secrets.choice(alphabet) for _ in range(32))
        return token


class OrganizationPackageMember(models.Model):
    """
    Members of an organization package purchase.
    All members (including the purchaser) can use sessions from the package.
    """
    package_purchase = models.ForeignKey(
        CoachingPackagePurchase,
        on_delete=models.CASCADE,
        related_name='organization_members',
        help_text="The organization package purchase this member belongs to"
    )
    phone = models.CharField(max_length=15, help_text="Phone number of the member")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='organization_package_memberships',
        help_text="User account if member has one (set when validated)"
    )
    added_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = [['package_purchase', 'phone']]
        ordering = ['added_at']
        verbose_name = 'Organization Package Member'
        verbose_name_plural = 'Organization Package Members'
    
    def __str__(self):
        return f"{self.phone} - {self.package_purchase.purchase_name}"


class TempPurchase(models.Model):
    """
    Temporary purchase record created before payment processing.
    Stores purchase details until webhook confirms payment.
    """
    PURCHASE_TYPE_CHOICES = (
        ('normal', 'Normal'),
        ('gift', 'Gift'),
        ('organization', 'Organization'),
    )
    
    temp_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        CoachingPackage,
        on_delete=models.CASCADE,
        related_name='temp_purchases',
        null=True,
        blank=True
    )
    simulator_package = models.ForeignKey(
        'coaching.SimulatorPackage',
        on_delete=models.CASCADE,
        related_name='temp_purchases',
        null=True,
        blank=True
    )
    buyer_phone = models.CharField(max_length=15)
    purchase_type = models.CharField(max_length=20, choices=PURCHASE_TYPE_CHOICES, default='normal')
    package_type = models.CharField(
        max_length=20,
        choices=[('coaching', 'Coaching'), ('simulator', 'Simulator')],
        null=True,
        blank=True,
        help_text="Type of package: 'coaching' or 'simulator'. Explicitly stored to avoid ambiguity when both package types exist with same ID."
    )
    recipients = models.JSONField(
        default=list,
        blank=True,
        null=False,
        help_text="List of recipient phone numbers (for gift/organization purchases)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Expiration time for temp purchase (default 24 hours)"
    )
    referral_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='referred_temp_purchases',
        help_text="Staff member who referred this purchase (optional)"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Temporary Purchase'
        verbose_name_plural = 'Temporary Purchases'
    
    def __str__(self):
        package_name = self.package.title if self.package else (self.simulator_package.title if self.simulator_package else 'Unknown')
        return f"TempPurchase {self.temp_id} - {self.buyer_phone} - {self.purchase_type} - {package_name}"
    
    def clean(self):
        from django.core.exceptions import ValidationError
        if not self.package and not self.simulator_package:
            raise ValidationError("Either package or simulator_package must be set.")
        if self.package and self.simulator_package:
            raise ValidationError("Cannot set both package and simulator_package.")
        # Ensure recipients is always a list (not None) for normal purchases
        if self.recipients is None:
            self.recipients = []
    
    def save(self, *args, **kwargs):
        from django.utils import timezone
        from datetime import timedelta
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)
    
    @property
    def is_expired(self):
        from django.utils import timezone
        if self.expires_at:
            return timezone.now() > self.expires_at
        return False


class PendingRecipient(models.Model):
    """
    Stores recipients who don't exist in the system yet.
    When they sign up, these records are converted to actual purchases.
    """
    PURCHASE_TYPE_CHOICES = (
        ('gift', 'Gift'),
        ('organization', 'Organization'),
    )
    
    STATUS_CHOICES = (
        ('pending', 'Pending'),     # waiting for recipient signup
        ('converted', 'Converted'), # after converting to real purchase
    )
    
    package = models.ForeignKey(
        CoachingPackage,
        on_delete=models.CASCADE,
        related_name="pending_recipients"
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='initiated_pending_recipients'
    )
    recipient_phone = models.CharField(max_length=15)
    purchase_type = models.CharField(max_length=20, choices=PURCHASE_TYPE_CHOICES)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    temp_purchase = models.ForeignKey(
        TempPurchase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pending_recipients',
        help_text="Reference to the temp purchase that created this pending recipient"
    )
    package_purchase = models.ForeignKey(
        CoachingPackagePurchase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pending_recipients_from_purchase',
        help_text="Direct link to the purchase this pending recipient belongs to. Set for organization purchases, None for gift purchases until recipient signs up."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Pending Recipient'
        verbose_name_plural = 'Pending Recipients'
        unique_together = [['package', 'buyer', 'recipient_phone', 'purchase_type']]
    
    def __str__(self):
        return f"Pending {self.purchase_type} - {self.recipient_phone} - Status: {self.status}"


class SimulatorPackage(models.Model):
    """
    Simulator-only packages that contain only simulator hours (no coaching sessions).
    """
    title = models.CharField(max_length=200)
    description = models.TextField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    location_id = models.CharField(max_length=100, blank=True, null=True, help_text="GHL location ID for this simulator package")
    hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Number of simulator hours included in this package"
    )
    redirect_url = models.URLField(max_length=500, blank=True, null=True, help_text="URL to redirect to after package purchase")
    is_active = models.BooleanField(default=True)
    validity_days = models.PositiveIntegerField(
        blank=True, 
        null=True, 
        help_text="Number of days from purchase date that this package is valid. If set, clients cannot use the package after this period."
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Simulator Package'
        verbose_name_plural = 'Simulator Packages'
    
    def __str__(self):
        return self.title
    
    @property
    def has_time_restrictions(self):
        """Check if this package has any time restrictions"""
        return self.time_restrictions.exists()
    
    def get_matching_restrictions(self, booking_datetime):
        """
        Get time restrictions that match the booking datetime.
        
        Args:
            booking_datetime: datetime object for the booking start time
            
        Returns:
            QuerySet of matching SimulatorPackageTimeRestriction objects
        """
        from django.utils import timezone
        from datetime import datetime
        
        if not isinstance(booking_datetime, datetime):
            return self.time_restrictions.none()
        
        booking_date = booking_datetime.date()
        booking_time = booking_datetime.time()
        booking_day_of_week = booking_datetime.weekday()  # 0=Monday, 6=Sunday
        
        matching_restrictions = []
        
        for restriction in self.time_restrictions.all():
            # Check if time falls within the restriction window
            # Note: end_time is exclusive (booking must start before end_time)
            if booking_time < restriction.start_time or booking_time >= restriction.end_time:
                continue
            
            if restriction.is_recurring:
                # Check if day of week matches
                if restriction.day_of_week is not None and restriction.day_of_week == booking_day_of_week:
                    matching_restrictions.append(restriction)
            else:
                # Check if date matches
                if restriction.date is not None and restriction.date == booking_date:
                    matching_restrictions.append(restriction)
        
        # Return a queryset-like list (we'll filter by IDs)
        if matching_restrictions:
            restriction_ids = [r.id for r in matching_restrictions]
            return self.time_restrictions.filter(id__in=restriction_ids)
        return self.time_restrictions.none()


class SimulatorPackageTimeRestriction(models.Model):
    """
    Time restrictions for simulator packages.
    Supports both recurring (day of week) and non-recurring (specific date) restrictions.
    """
    DAY_CHOICES = (
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    )
    
    package = models.ForeignKey(
        SimulatorPackage,
        on_delete=models.CASCADE,
        related_name='time_restrictions'
    )
    is_recurring = models.BooleanField(
        default=True,
        help_text="If True, this is a recurring restriction (day of week). If False, it's a specific date."
    )
    day_of_week = models.IntegerField(
        choices=DAY_CHOICES,
        null=True,
        blank=True,
        help_text="Day of week (0=Monday, 6=Sunday). Only used if is_recurring=True."
    )
    date = models.DateField(
        null=True,
        blank=True,
        help_text="Specific date for non-recurring restriction. Only used if is_recurring=False."
    )
    start_time = models.TimeField(help_text="Start time for this restriction")
    end_time = models.TimeField(help_text="End time for this restriction")
    limit_hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=1.0,
        help_text="Maximum number of hours this package can be used on this day/date within the time window"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['package', 'is_recurring', 'day_of_week', 'date', 'start_time']
        verbose_name = 'Simulator Package Time Restriction'
        verbose_name_plural = 'Simulator Package Time Restrictions'
        unique_together = [
            ['package', 'is_recurring', 'day_of_week', 'start_time'],  # For recurring restrictions
            ['package', 'is_recurring', 'date', 'start_time'],  # For non-recurring restrictions
        ]
    
    def __str__(self):
        if self.is_recurring:
            day_name = self.get_day_of_week_display() if self.day_of_week is not None else 'Unknown'
            return f"{self.package.title} - {day_name} ({self.start_time} - {self.end_time}) - Limit: {self.limit_hours} hrs"
        else:
            return f"{self.package.title} - {self.date} ({self.start_time} - {self.end_time}) - Limit: {self.limit_hours} hrs"
    
    def clean(self):
        from django.core.exceptions import ValidationError
        if self.is_recurring:
            if self.day_of_week is None:
                raise ValidationError("day_of_week is required for recurring restrictions.")
            if self.date is not None:
                raise ValidationError("date should not be set for recurring restrictions.")
        else:
            if self.date is None:
                raise ValidationError("date is required for non-recurring restrictions.")
            if self.day_of_week is not None:
                raise ValidationError("day_of_week should not be set for non-recurring restrictions.")


class SimulatorPackagePurchase(models.Model):
    """
    Purchase record for simulator-only packages.
    Supports transfer (partial hours) and gift (entire package) functionality.
    """
    PURCHASE_TYPE_CHOICES = (
        ('normal', 'Normal'),
        ('gift', 'Gift'),
    )
    
    GIFT_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    )
    
    PACKAGE_STATUS_CHOICES = (
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('gifted', 'Gifted'),
    )
    
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='simulator_package_purchases'
    )
    package = models.ForeignKey(
        SimulatorPackage,
        on_delete=models.CASCADE,
        related_name='purchases'
    )
    purchase_name = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Custom label for this purchase"
    )
    hours_total = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Total simulator hours in this purchase"
    )
    hours_remaining = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Remaining simulator hours in this purchase"
    )
    notes = models.CharField(max_length=255, blank=True)
    
    # Gifting fields
    purchase_type = models.CharField(max_length=15, choices=PURCHASE_TYPE_CHOICES, default='normal')
    recipient_phone = models.CharField(max_length=15, blank=True, null=True, help_text="Phone number of gift recipient")
    gift_status = models.CharField(max_length=10, choices=GIFT_STATUS_CHOICES, blank=True, null=True)
    gift_token = models.CharField(max_length=64, unique=True, blank=True, null=True, help_text="Unique token for gift claim")
    original_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='gifted_simulator_packages',
        help_text="Original purchaser for gifted packages"
    )
    package_status = models.CharField(max_length=10, choices=PACKAGE_STATUS_CHOICES, default='active')
    
    purchased_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    gift_expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiration date for gift claim")
    expiry_date = models.DateField(null=True, blank=True, help_text="Expiry date for this purchase. After this date, the package cannot be used.")
    
    class Meta:
        ordering = ['-purchased_at']
        verbose_name = 'Simulator Package Purchase'
        verbose_name_plural = 'Simulator Package Purchases'
    
    def __str__(self):
        return f"{self.purchase_name} - {self.package.title} ({self.hours_remaining}/{self.hours_total} hrs)"
    
    def generate_gift_token(self):
        """Generate a unique gift claim token"""
        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))
        # Ensure uniqueness
        while SimulatorPackagePurchase.objects.filter(gift_token=token).exists():
            token = ''.join(secrets.choice(alphabet) for _ in range(32))
        return token
    
    @property
    def is_depleted(self):
        return self.hours_remaining <= 0
    
    @property
    def is_gift_pending(self):
        return self.purchase_type == 'gift' and self.gift_status == 'pending'
    
    @property
    def can_be_transferred(self):
        """Check if package can have hours transferred"""
        return (
            self.package_status == 'active' and
            self.hours_remaining > 0 and
            not self.is_gift_pending
        )
    
    @property
    def is_expired(self):
        """Check if the package purchase has expired"""
        from django.utils import timezone
        if self.expiry_date:
            return timezone.now().date() > self.expiry_date
        return False
    
    @property
    def can_be_used(self):
        """Check if package can be used (not expired, not depleted, active status)"""
        return (
            self.package_status == 'active' and
            self.hours_remaining > 0 and
            not self.is_expired
        )
    
    def consume_hours(self, hours):
        """
        Consume simulator hours from this purchase.
        
        Args:
            hours: Decimal or float representing hours to consume
        """
        from decimal import Decimal
        hours = Decimal(str(hours))
        if hours <= 0:
            raise ValueError("hours must be greater than 0")
        if self.hours_remaining < hours:
            raise ValueError("Not enough simulator hours remaining")
        self.hours_remaining -= hours
        if self.hours_remaining <= 0:
            self.package_status = 'completed'
        self.save(update_fields=['hours_remaining', 'package_status', 'updated_at'])


class SimulatorPackageUsage(models.Model):
    """
    Track usage of time-restricted simulator packages.
    Records each booking that uses a restricted package to enforce daily hour limits.
    """
    package_purchase = models.ForeignKey(
        SimulatorPackagePurchase,
        on_delete=models.CASCADE,
        related_name='usage_records'
    )
    booking = models.ForeignKey(
        'bookings.Booking',
        on_delete=models.CASCADE,
        related_name='package_usage_records',
        null=True,
        blank=True
    )
    restriction = models.ForeignKey(
        SimulatorPackageTimeRestriction,
        on_delete=models.CASCADE,
        related_name='usage_records'
    )
    usage_date = models.DateField(help_text="Date when the package was used")
    usage_time = models.TimeField(help_text="Time when the package was used")
    hours_used = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Number of hours used in this booking"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-usage_date', '-usage_time']
        verbose_name = 'Simulator Package Usage'
        verbose_name_plural = 'Simulator Package Usages'
        indexes = [
            models.Index(fields=['package_purchase', 'usage_date', 'restriction']),
        ]
    
    def __str__(self):
        return f"{self.package_purchase.package.title} - {self.usage_date} {self.usage_time}"


class SimulatorHoursTransfer(models.Model):
    """
    Transfer of simulator hours from one user to another.
    Similar to SessionTransfer but for simulator-only packages.
    """
    TRANSFER_STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    )
    
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_simulator_transfers'
    )
    to_user_phone = models.CharField(max_length=15, help_text="Phone number of transfer recipient")
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_simulator_transfers',
        help_text="Set when recipient accepts transfer"
    )
    package_purchase = models.ForeignKey(
        SimulatorPackagePurchase,
        on_delete=models.CASCADE,
        related_name='transfers'
    )
    hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Number of hours to transfer"
    )
    transfer_status = models.CharField(max_length=10, choices=TRANSFER_STATUS_CHOICES, default='pending')
    transfer_token = models.CharField(max_length=64, unique=True, help_text="Unique token for transfer claim")
    notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiration date for transfer claim")
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Simulator Hours Transfer'
        verbose_name_plural = 'Simulator Hours Transfers'
    
    def __str__(self):
        return f"{self.from_user.username} → {self.to_user_phone} ({self.hours} hrs)"
    
    def generate_transfer_token(self):
        """Generate a unique transfer claim token"""
        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))
        # Ensure uniqueness
        while SimulatorHoursTransfer.objects.filter(transfer_token=token).exists():
            token = ''.join(secrets.choice(alphabet) for _ in range(32))
        return token