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
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Simulator Package'
        verbose_name_plural = 'Simulator Packages'
    
    def __str__(self):
        return self.title


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