from django.utils.text import slugify
from rest_framework import serializers

from .models import ServiceCategory


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
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

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
