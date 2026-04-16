from django.db import models


class ServiceCategory(models.Model):
    """
    Bookable / purchasable service grouping for a facility (Phase A: read-only API + legacy mapping).

    legacy_booking_type links this row to the existing simulator/coaching booking stack.
    Rows with legacy_booking_type NULL are reserved for future phases (dynamic resource rules).
    """

    LEGACY_BOOKING_TYPE_CHOICES = (
        ('simulator', 'Simulator'),
        ('coaching', 'Coaching'),
    )

    name = models.CharField(max_length=120, help_text="Display name in admin and API")
    slug = models.SlugField(max_length=80, help_text="Stable key within a location scope")
    customer_label = models.CharField(
        max_length=120,
        help_text="Short label shown on the customer booking UI",
    )
    description = models.TextField(blank=True)
    # Empty string = fallback categories for any location that has no location-specific rows
    location_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        db_index=True,
        help_text="GHL location id; empty string = default/fallback for all locations",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    legacy_booking_type = models.CharField(
        max_length=20,
        choices=LEGACY_BOOKING_TYPE_CHOICES,
        null=True,
        blank=True,
        help_text="Phase A: maps to existing booking_type. Null = not driven by legacy tabs yet.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'slug'],
                name='categories_servicecategory_location_slug_uniq',
            ),
        ]

    def __str__(self):
        loc = self.location_id or '(default)'
        return f"{self.name} @ {loc}"
