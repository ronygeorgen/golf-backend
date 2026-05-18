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


# ---------------------------------------------------------------------------
# Category Assets
# ---------------------------------------------------------------------------

class CategoryAsset(models.Model):
    """
    A bookable physical asset belonging to a ServiceCategory.

    One row = one physical unit (e.g. "Table Tennis Table 1", "Fitness Room A").
    Availability for needs_staff=False assets is driven by CategoryAssetAvailability.
    Availability for needs_staff=True assets is driven by StaffAvailability (existing system).

    Single booking per slot per asset — identical semantics to the Simulator model.
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

    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.CASCADE,
        related_name='assets',
    )
    name = models.CharField(max_length=120, help_text="Display name, e.g. 'Table Tennis Table 1'")
    price_per_hour = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Hourly rate in USD. Leave blank if not applicable.",
    )
    needs_staff = models.BooleanField(
        default=False,
        help_text="If True, slot availability is determined by staff schedule. "
                  "If False, availability is determined by this asset's own schedule.",
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    description = models.TextField(blank=True)
    location_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        db_index=True,
        help_text="GHL location id; empty = default/fallback",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.category.name})"


class CategoryAssetAvailability(models.Model):
    """
    Weekly recurring availability schedule for a CategoryAsset.

    Only consulted when the asset's needs_staff=False.
    Pattern mirrors SimulatorAvailability: one row per day/start_time window.
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

    asset = models.ForeignKey(
        CategoryAsset,
        on_delete=models.CASCADE,
        related_name='availabilities',
    )
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        unique_together = ['asset', 'day_of_week', 'start_time']
        verbose_name_plural = 'Category Asset Availabilities'

    def __str__(self):
        return f"{self.asset.name} – {self.get_day_of_week_display()} ({self.start_time}–{self.end_time})"
