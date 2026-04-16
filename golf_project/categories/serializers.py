from rest_framework import serializers

from .models import ServiceCategory


class ServiceCategorySerializer(serializers.ModelSerializer):
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
