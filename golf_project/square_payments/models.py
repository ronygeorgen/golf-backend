from django.db import models
from django.utils import timezone
from django.conf import settings


class LocationSquareAccount(models.Model):
    """
    Stores Square OAuth credentials on a per-GHL-location basis.

    Each golf-center (GHLLocation) can connect its own Square account.
    When a customer pays, we look up the location's access_token and use
    that to charge the customer — so money lands directly in the
    golf-center's Square account, NOT in the platform account.
    """

    # Link to the GHL location record — this is how we identify which
    # golf center owns these Square credentials.
    ghl_location = models.OneToOneField(
        'ghl.GHLLocation',
        on_delete=models.CASCADE,
        related_name='square_account',
        help_text="The GHL Location (golf center) this Square account belongs to.",
    )

    # Square merchant info
    merchant_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Square merchant_id returned during OAuth.",
    )

    # OAuth tokens
    access_token = models.TextField(
        blank=True,
        help_text="Square OAuth access token (used for all API calls).",
    )
    refresh_token = models.TextField(
        blank=True,
        help_text="Square OAuth refresh token (use to get a new access_token).",
    )
    token_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the current access_token expires.",
    )

    # Square location details (fetched after OAuth)
    square_location_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Square Location ID for this merchant (used in payment calls).",
    )
    square_location_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Human-readable name of the Square location.",
    )

    # Status
    is_connected = models.BooleanField(
        default=False,
        help_text="True once the golf center has successfully completed OAuth.",
    )

    connected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-connected_at']
        verbose_name = 'Location Square Account'
        verbose_name_plural = 'Location Square Accounts'

    def __str__(self):
        location_name = getattr(self.ghl_location, 'company_name', None) or self.ghl_location.location_id
        return f"{location_name} — Square ({self.merchant_id or 'not connected'})"

    # ------------------------------------------------------------------
    # Token helpers (mirrors GHLLocation pattern)
    # ------------------------------------------------------------------

    def is_token_valid(self) -> bool:
        """Return True if the access token exists and has not expired."""
        if not self.access_token or not self.token_expires_at:
            return False
        return timezone.now() < self.token_expires_at

    def needs_token_refresh(self) -> bool:
        """
        Return True if the token is missing or will expire within 30 minutes.
        Square access tokens are valid for 30 days, but we refresh proactively.
        """
        if not self.token_expires_at:
            return True
        return timezone.now() >= (self.token_expires_at - timezone.timedelta(minutes=30))


class MemberSubscription(models.Model):
    """
    Tracks a client's Square recurring subscription for a Membership-type SimulatorPackage.

    Each billing cycle Square fires an invoice.payment_made webhook which resets
    the linked SimulatorPackagePurchase hours_remaining to the package's monthly_hours.
    No carry-over — hours reset completely each period.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('canceled', 'Canceled'),
    ]

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='member_subscriptions',
        help_text='The user who subscribed.',
    )
    package = models.ForeignKey(
        'coaching.SimulatorPackage',
        on_delete=models.PROTECT,
        related_name='member_subscriptions',
        null=True,
        blank=True,
        help_text='The membership-type SimulatorPackage (if applicable).',
    )
    purchase = models.OneToOneField(
        'coaching.SimulatorPackagePurchase',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member_subscription',
        help_text="The SimulatorPackagePurchase that holds this member's current hours (if applicable).",
    )
    coaching_package = models.ForeignKey(
        'coaching.CoachingPackage',
        on_delete=models.PROTECT,
        related_name='member_subscriptions',
        null=True,
        blank=True,
        help_text='The membership-type CoachingPackage (if applicable).',
    )
    coaching_purchase = models.OneToOneField(
        'coaching.CoachingPackagePurchase',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='member_subscription',
        help_text="The CoachingPackagePurchase that holds this member's current hours/sessions (if applicable).",
    )
    ghl_location_id = models.CharField(
        max_length=100,
        help_text='GHL location ID — used to look up the Square OAuth token for billing.',
    )
    square_subscription_id = models.CharField(
        max_length=100,
        unique=True,
        help_text='Square subscription ID returned by CreateSubscription.',
    )
    square_plan_variation_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Square Catalog plan variation ID for this subscription plan.',
    )
    square_customer_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Square customer ID for this member.',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
    )
    current_period_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Start of the current billing period.',
    )
    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text='End of the current billing period (next reset/charge date).',
    )
    canceled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the subscription was canceled.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Member Subscription'
        verbose_name_plural = 'Member Subscriptions'

    def __str__(self):
        pkg_title = self.package.title if self.package else (self.coaching_package.title if self.coaching_package else "Unknown Package")
        return f'{self.client.username} — {pkg_title} ({self.status})'

    def is_subscription_active(self):
        return self.status == 'active'

