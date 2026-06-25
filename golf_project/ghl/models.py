from django.db import models
from django.utils import timezone


class GHLLocation(models.Model):
    location_id = models.CharField(max_length=100, unique=True)
    company_name = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(
        max_length=100,
        default='America/Halifax',
        help_text="IANA timezone for this golf center (e.g. America/Halifax, America/Toronto, Asia/Dubai)"
    )
    logo = models.ImageField(
        upload_to='location_logos/',
        null=True,
        blank=True,
        help_text="Company logo (912×273 recommended, max 1 MB)"
    )
    # Invoice / contact details — shown on payment receipt emails
    contact_phone = models.CharField(
        max_length=50,
        blank=True,
        help_text="Location phone number shown on invoice emails (e.g. +1 902-555-0100)."
    )
    support_email = models.EmailField(
        blank=True,
        help_text="Location support email shown on invoice emails (e.g. support@mygolfcenter.com)."
    )
    business_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Business registration / GST-HST number shown on invoice emails."
    )
    refund_policy = models.TextField(
        blank=True,
        help_text="Refund / cancellation policy text shown on invoice emails."
    )
    status = models.CharField(max_length=50, blank=True)
    webhook_url = models.URLField(blank=True)
    webhook_secret = models.CharField(max_length=255, blank=True)
    # OAuth tokens
    access_token = models.TextField(blank=True, help_text="OAuth access token")
    refresh_token = models.TextField(blank=True, help_text="OAuth refresh token")
    token_expires_at = models.DateTimeField(null=True, blank=True, help_text="When the access token expires")
    metadata = models.JSONField(default=dict, blank=True)
    onboarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.company_name or 'GHL Location'} ({self.location_id})"
    
    def is_token_valid(self):
        """Check if the access token is still valid"""
        if not self.access_token or not self.token_expires_at:
            return False
        return timezone.now() < self.token_expires_at
    
    def needs_token_refresh(self):
        """Check if token needs to be refreshed (within 5 minutes of expiry)"""
        if not self.token_expires_at:
            return True
        # Refresh if token expires within 5 minutes
        return timezone.now() >= (self.token_expires_at - timezone.timedelta(minutes=5))

