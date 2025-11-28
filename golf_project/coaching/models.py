from django.conf import settings
from django.db import models
import secrets
import string


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
        return f"{self.purchase_name} - {self.package.title} ({self.sessions_remaining}/{self.sessions_total})"
    
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
        return self.sessions_remaining <= 0
    
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
        if self.sessions_remaining == 0:
            self.package_status = 'completed'
        self.save(update_fields=['sessions_remaining', 'package_status', 'updated_at'])


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