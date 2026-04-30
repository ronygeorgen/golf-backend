from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone


class Coupon(models.Model):
    DISCOUNT_TYPES = (
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    )

    APPLICABLE_CHOICES = (
        ('simulator', 'Simulator Bookings'),
        ('package', 'Package Purchases'),
        ('event', 'Special Event Registrations'),
        ('asset', 'Generic Asset Bookings'),
    )

    code = models.CharField(max_length=50, unique=True, db_index=True)
    description = models.TextField(blank=True, null=True)
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPES, default='percentage')
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    
    applicable_to = models.CharField(
        max_length=255,
        default='all',
        help_text="Comma-separated list of applicable types (simulator, package, event, asset) or 'all'."
    )

    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Total times this coupon can be used.")
    uses_count = models.PositiveIntegerField(default=0)
    per_user_limit = models.PositiveIntegerField(null=True, blank=True, default=1)

    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.code} ({self.discount_value} {self.discount_type})"

    def is_valid(self, payment_type=None, user=None, email=None, phone=None):
        """Checks if coupon is globally valid + matches payment_type + respects per-user limits."""
        now = timezone.now()

        if not self.is_active:
            return False, "This coupon is no longer active."

        if self.valid_from and now < self.valid_from:
            return False, f"This coupon is valid from {self.valid_from.strftime('%Y-%m-%d %H:%M')}."

        if self.valid_until and now > self.valid_until:
            return False, "This coupon has expired."

        if self.max_uses is not None and self.uses_count >= self.max_uses:
            return False, "This coupon has reached its maximum usage limit."

        # Purpose check
        if payment_type and self.applicable_to != 'all':
            allowed_types = [t.strip() for t in self.applicable_to.split(',') if t.strip()]
            
            # 1. Exact match (e.g. 'simulator' in ['simulator', 'package'])
            if payment_type in allowed_types:
                return True, ""
            
            # 2. General 'asset' match for specific asset requests (e.g. 'asset:5' matches 'asset')
            if payment_type.startswith('asset:') and 'asset' in allowed_types:
                return True, ""
            
            # 3. None match
            purpose_map = dict(self.APPLICABLE_CHOICES)
            # If it's a specific asset, try to find a nice label if it's in the allowed list
            allowed_labels = []
            for t in allowed_types:
                if t.startswith('asset:'):
                    try:
                        from categories.models import CategoryAsset
                        asset_id = t.split(':')[1]
                        asset_obj = CategoryAsset.objects.get(id=asset_id)
                        allowed_labels.append(f"Asset: {asset_obj.name}")
                    except Exception:
                        allowed_labels.append(purpose_map.get(t, t))
                else:
                    allowed_labels.append(purpose_map.get(t, t))

            return False, f"This coupon is only valid for: {', '.join(allowed_labels)}."

        # Per-user limit check (if info provided)
        if self.per_user_limit:
            # Check by user, email, or phone
            user_filters = []
            if user and not user.is_anonymous:
                user_filters.append(models.Q(user=user))
            if email:
                user_filters.append(models.Q(customer_email__iexact=email))
            if phone:
                user_filters.append(models.Q(customer_phone=phone))
            
            if user_filters:
                from django.db.models import Q
                combined_filter = Q()
                for f in user_filters:
                    combined_filter |= f
                
                user_uses = CouponUsage.objects.filter(Q(coupon=self) & combined_filter).count()
                if user_uses >= self.per_user_limit:
                    return False, "You have already used this coupon the maximum number of times."

        return True, ""

    def calculate_discount(self, original_amount: float) -> float:
        """Returns the discount amount (not the final price)."""
        if self.discount_type == 'percentage':
            discount = original_amount * float(self.discount_value) / 100
            return round(min(discount, original_amount), 2)  # Never exceed original
        else:
            return round(min(float(self.discount_value), original_amount), 2)


class CouponUsage(models.Model):
    """Records every time a coupon is used, for audit and per-user limit enforcement."""
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='usages')
    user = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True)
    customer_email = models.EmailField(max_length=255, null=True, blank=True)
    customer_phone = models.CharField(max_length=20, null=True, blank=True)
    
    payment_id = models.CharField(max_length=255, blank=True, help_text="Square payment ID or temp_id")
    payment_type = models.CharField(max_length=20, blank=True, help_text="simulator / package / event")
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    original_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    item_label = models.CharField(max_length=255, blank=True, help_text="Specific item name (e.g. Package Title)")
    used_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-used_at']

    def __str__(self):
        return f"{self.coupon.code} used by {self.user or self.customer_email or self.customer_phone or 'Guest'}"
