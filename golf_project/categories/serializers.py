from django.utils.text import slugify
from rest_framework import serializers

from .models import CategoryAsset, CategoryAssetAvailability, ServiceCategory


class ServiceCategorySerializer(serializers.ModelSerializer):
    """Read-only serializer used by the public /categories/active/ endpoint."""

    class Meta:
        model = ServiceCategory
        fields = (
            'id',
            'name',
            'slug',
            'customer_label',
            'description',
            'location_id',
            'sort_order',
            'is_active',
            'legacy_booking_type',
        )
        read_only_fields = fields


class ServiceCategoryAdminSerializer(serializers.ModelSerializer):
    """Writable serializer for the admin CRUD endpoint."""

    # Phase D: how many staff are assigned to this category
    staff_count = serializers.SerializerMethodField()

    class Meta:
        model = ServiceCategory
        fields = (
            'id',
            'name',
            'slug',
            'customer_label',
            'description',
            'location_id',
            'sort_order',
            'is_active',
            'legacy_booking_type',
            'staff_count',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'staff_count', 'created_at', 'updated_at')

    def get_staff_count(self, obj):
        return obj.staff_assignments.count()

    def validate_slug(self, value):
        if value:
            return value.lower().strip()
        return value

    def validate(self, attrs):
        # Auto-generate slug from name when omitted
        if not attrs.get('slug'):
            attrs['slug'] = slugify(attrs.get('name', ''))[:80]

        # Ensure slug is unique within the same location scope (excluding current instance)
        location_id = attrs.get(
            'location_id',
            getattr(self.instance, 'location_id', '') if self.instance else '',
        )
        slug = attrs['slug']
        qs = ServiceCategory.objects.filter(location_id=location_id, slug=slug)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {'slug': f'A category with slug "{slug}" already exists for this location.'}
            )
        return attrs


# ---------------------------------------------------------------------------
# Category Assets
# ---------------------------------------------------------------------------

class CategoryAssetAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryAssetAvailability
        fields = ('id', 'asset', 'day_of_week', 'start_time', 'end_time')

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        for f in ('start_time', 'end_time'):
            val = rep.get(f)
            if val and isinstance(val, str) and val.count(':') > 1:
                rep[f] = val[:5]
        return rep


class CategoryAssetSerializer(serializers.ModelSerializer):
    availabilities = CategoryAssetAvailabilitySerializer(many=True, read_only=True)
    asset_count = serializers.SerializerMethodField()

    class Meta:
        model = CategoryAsset
        fields = (
            'id', 'category', 'name', 'price_per_hour', 'needs_staff',
            'is_active', 'sort_order', 'description', 'location_id',
            'created_at', 'updated_at', 'availabilities', 'asset_count',
        )
        read_only_fields = ('id', 'created_at', 'updated_at', 'availabilities', 'asset_count')

    def get_asset_count(self, obj):
        """Returns how many active bookings exist for this asset (for display purposes)."""
        return 0  # placeholder — bookings query added after Booking model is updated
