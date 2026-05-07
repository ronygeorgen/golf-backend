from django.db import models
from django.utils import timezone


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
