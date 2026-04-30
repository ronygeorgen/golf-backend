from rest_framework import serializers
from .models import Coupon, CouponUsage


class CouponSerializer(serializers.ModelSerializer):
    uses_count = serializers.ReadOnlyField()
    is_currently_valid = serializers.SerializerMethodField()
    remaining_uses = serializers.SerializerMethodField()

    class Meta:
        model = Coupon
        fields = [
            'id', 'code', 'description', 'discount_type', 'discount_value',
            'applicable_to',
            'max_uses', 'uses_count', 'remaining_uses',
            'per_user_limit', 'valid_from', 'valid_until',
            'is_active', 'is_currently_valid',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['uses_count', 'created_at', 'updated_at']

    def get_is_currently_valid(self, obj):
        valid, _ = obj.is_valid()
        return valid

    def get_remaining_uses(self, obj):
        if obj.max_uses is None:
            return None  # unlimited
        return max(0, obj.max_uses - obj.uses_count)

    def validate_code(self, value):
        return value.upper().strip()

    def validate_discount_value(self, value):
        return value

    def validate(self, data):
        discount_type = data.get('discount_type', getattr(self.instance, 'discount_type', 'percentage'))
        discount_value = data.get('discount_value', getattr(self.instance, 'discount_value', 0))
        if discount_type == 'percentage' and float(discount_value) > 100:
            raise serializers.ValidationError({'discount_value': 'Percentage discount cannot exceed 100%.'})
        return data


class CouponUsageSerializer(serializers.ModelSerializer):
    coupon_code = serializers.CharField(source='coupon.code', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = CouponUsage
        fields = [
            'id', 'coupon', 'coupon_code', 'user', 'user_name',
            'customer_email', 'customer_phone',
            'payment_id', 'payment_type', 'item_label',
            'discount_amount', 'original_amount', 'final_amount',
            'used_at',
        ]

    def get_user_name(self, obj):
        if obj.user:
            return obj.user.get_full_name() or obj.user.phone
        return obj.customer_email or obj.customer_phone or 'Guest'


class CouponValidateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    # payment_type is a free string — validation against applicable_to is done in is_valid()
    payment_type = serializers.CharField(max_length=30, required=False)
