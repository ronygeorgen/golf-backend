from decimal import Decimal, ROUND_HALF_UP
from rest_framework import serializers
from datetime import timedelta
from .models import Booking
from users.serializers import UserSerializer
from simulators.serializers import SimulatorSerializer, SimulatorCreditSerializer
from coaching.serializers import (
    CoachingPackageSerializer, CoachingPackagePurchaseSerializer,
    SimulatorPackagePurchaseSerializer
)

class BookingCreateSerializer(serializers.ModelSerializer):
    use_simulator_credit = serializers.BooleanField(write_only=True, required=False, default=False)
    use_organization_package = serializers.BooleanField(write_only=True, required=False, default=False)
    use_prepaid_hours = serializers.BooleanField(write_only=True, required=False, default=None, allow_null=True)
    simulator_count = serializers.IntegerField(write_only=True, required=False, default=1, min_value=1)
    # Staff/admin only: honored in BookingViewSet.perform_create and validate() to skip coach-capacity,
    # same-coach conflict, and special-event overlap checks for coaching bookings.
    admin_manual_booking = serializers.BooleanField(write_only=True, required=False, default=False)
    location_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)  # Allow location_id to be passed or set in perform_create
    
    class Meta:
        model = Booking
        fields = [
            'booking_type', 'simulator', 'duration_minutes',
            'coaching_package', 'coach', 'start_time', 'end_time', 'total_price',
            'use_simulator_credit', 'use_organization_package', 'use_prepaid_hours', 'simulator_count',
            'admin_manual_booking',
            'location_id',
            'service_category',
            'category_asset',
        ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make total_price optional - it will be set automatically in validate()
        if 'total_price' in self.fields:
            self.fields['total_price'].required = False
    
    def _request_user_can_admin_manual_booking(self):
        """Must match BookingViewSet._request_user_can_force_coaching_booking() logic."""
        request = self.context.get('request') if hasattr(self, 'context') else None
        if not request or not getattr(request.user, 'is_authenticated', False):
            return False
        user = request.user
        if getattr(user, 'is_superuser', False):
            return True
        role = getattr(user, 'role', '') or ''
        return role in ('admin', 'staff', 'superadmin')
    
    def validate(self, data):
        # Check for booking conflicts
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        simulator = data.get('simulator')
        coach = data.get('coach')
        booking_type = data.get('booking_type')
        coaching_package = data.get('coaching_package')
        
        if start_time and end_time:
            if start_time >= end_time:
                raise serializers.ValidationError("End time must be after start time")
            
            # Check if facility is closed during booking time
            from admin_panel.models import ClosedDay
            location_id = self.context.get('location_id') if hasattr(self, 'context') else None
            
            # Check start time
            is_closed_start, message_start = ClosedDay.check_if_closed(start_time, location_id=location_id)
            if is_closed_start:
                raise serializers.ValidationError({
                    'start_time': message_start or "Facility is closed at the selected start time."
                })
            
            # Check end time
            is_closed_end, message_end = ClosedDay.check_if_closed(end_time, location_id=location_id)
            if is_closed_end:
                raise serializers.ValidationError({
                    'end_time': message_end or "Facility is closed at the selected end time."
                })
            
            # Check if any time during the booking overlaps with closed period
            # Check every 15 minutes during the booking
            current_check = start_time
            while current_check < end_time:
                is_closed, message = ClosedDay.check_if_closed(current_check, location_id=location_id)
                if is_closed:
                    raise serializers.ValidationError({
                        'start_time': message or "Facility is closed during the selected time period."
                    })
                current_check += timedelta(minutes=15)
            
            # Check for special event conflicts (privileged coaching force-book may override)
            coaching_force_special_override = (
                data.get('booking_type') == 'coaching'
                and data.get('admin_manual_booking') is True
                and self._request_user_can_admin_manual_booking()
            )
            if not coaching_force_special_override:
                from special_events.models import SpecialEvent
                active_events = SpecialEvent.objects.filter(is_active=True)
                if location_id:
                    active_events = active_events.filter(location_id=location_id)
                
                for event in active_events:
                    if event.conflicts_with_range(start_time, end_time):
                        raise serializers.ValidationError({
                            'start_time': f"This time slot conflicts with a special event: {event.title}."
                        })
            
            # Check for overlapping bookings
            conflicting_bookings = Booking.objects.filter(
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed']
            )
            exclude_id = self.context.get('exclude_booking_id')
            if exclude_id:
                conflicting_bookings = conflicting_bookings.exclude(id=exclude_id)
            
            if booking_type == 'simulator' and simulator:
                # Check for ALL booking types (simulator and coaching) on this simulator
                # because coaching sessions can use regular simulator bays when coaching bays are full
                conflicting_bookings = conflicting_bookings.filter(
                    simulator=simulator
                )
                if conflicting_bookings.exists():
                    raise serializers.ValidationError("This time slot is already booked for the selected simulator")
            
            # Note: Coach conflict check is moved to perform_create() in views.py
            # to prevent race conditions using select_for_update() locking
        
        # Booking-type specific validation
        if booking_type == 'coaching':
            if data.get('use_simulator_credit'):
                raise serializers.ValidationError("Simulator credits cannot be applied to coaching bookings.")

            # For dynamic category bookings on a needs_staff=False asset, coach is optional
            category_asset = data.get('category_asset')
            is_asset_only = category_asset and not category_asset.needs_staff
            if not is_asset_only and not coaching_package:
                raise serializers.ValidationError("A coaching package is required for coaching bookings.")

            # Asset-only bookings: check for asset conflicts
            if is_asset_only and start_time and end_time:
                asset_conflict = Booking.objects.filter(
                    category_asset=category_asset,
                    start_time__lt=end_time,
                    end_time__gt=start_time,
                    status__in=['confirmed', 'completed'],
                )
                if exclude_id := self.context.get('exclude_booking_id'):
                    asset_conflict = asset_conflict.exclude(id=exclude_id)
                if asset_conflict.exists():
                    raise serializers.ValidationError("This asset is already booked for the selected time slot.")

            if coaching_package:
                session_duration = coaching_package.session_duration_minutes
                if data.get('duration_minutes') and data['duration_minutes'] != session_duration:
                    raise serializers.ValidationError(
                        f"Coaching sessions must be {session_duration} minutes for the selected package."
                    )
                data['duration_minutes'] = session_duration
                if coaching_package.session_count:
                    per_session = (Decimal(coaching_package.price) / Decimal(coaching_package.session_count)).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                else:
                    per_session = Decimal(coaching_package.price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                data['total_price'] = per_session
            elif is_asset_only:
                # Price based on asset hourly rate and duration
                duration = data.get('duration_minutes') or 60
                if category_asset.price_per_hour:
                    data['total_price'] = (Decimal(category_asset.price_per_hour) * Decimal(duration) / 60).quantize(
                        Decimal('0.01'), rounding=ROUND_HALF_UP
                    )
                else:
                    data['total_price'] = Decimal('0.00')
        elif booking_type == 'simulator':
            data['use_simulator_credit'] = bool(data.get('use_simulator_credit'))
            if data.get('use_simulator_credit'):
                data['total_price'] = 0
        else:
            data['use_simulator_credit'] = False
        
        return data
    
    def create(self, validated_data):
        validated_data.pop('use_simulator_credit', None)
        validated_data.pop('use_organization_package', None)
        validated_data.pop('use_prepaid_hours', None)
        validated_data.pop('simulator_count', None)
        validated_data.pop('admin_manual_booking', None)
        return super().create(validated_data)

class BookingSerializer(serializers.ModelSerializer):
    client_details = UserSerializer(source='client', read_only=True)
    simulator_details = SimulatorSerializer(source='simulator', read_only=True)
    coach_details = UserSerializer(source='coach', read_only=True)
    package_details = CoachingPackageSerializer(source='coaching_package', read_only=True)
    package_purchase_details = CoachingPackagePurchaseSerializer(source='package_purchase', read_only=True)
    simulator_package_purchase_details = SimulatorPackagePurchaseSerializer(source='simulator_package_purchase', read_only=True)
    simulator_credit_details = SimulatorCreditSerializer(source='simulator_credit_redemption', read_only=True)
    uses_simulator_credit = serializers.SerializerMethodField()
    coaching_session_price = serializers.SerializerMethodField()
    purchase_type_label = serializers.SerializerMethodField()
    service_category_name = serializers.SerializerMethodField()
    category_asset_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ['client', 'created_at', 'updated_at']

    def get_uses_simulator_credit(self, obj):
        return obj.simulator_credit_redemption_id is not None

    def get_coaching_session_price(self, obj):
        if obj.booking_type != 'coaching':
            return None
        if obj.total_price:
            return obj.total_price
        package = getattr(obj, 'coaching_package', None)
        if package and package.session_count:
            value = (Decimal(package.price) / Decimal(package.session_count)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
            return value
        return Decimal(package.price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if package else None
    
    def get_purchase_type_label(self, obj):
        """Get human-readable label for purchase type"""
        if not obj.package_purchase or obj.booking_type != 'coaching':
            return None
        
        purchase_type = obj.package_purchase.purchase_type
        if purchase_type == 'gift':
            return 'Gifted'
        elif purchase_type == 'organization':
            return 'Organization'
        elif purchase_type == 'normal':
            # Check if it's from a transfer - look for accepted transfers for this user and package
            from coaching.models import SessionTransfer
            if obj.coaching_package:
                transfer = SessionTransfer.objects.filter(
                    to_user=obj.client,
                    package_purchase__package=obj.coaching_package,
                    transfer_status='accepted'
                ).first()
                if transfer:
                    return 'Transferred'
            return 'Personal'
        
        return 'Personal'

    def get_service_category_name(self, obj):
        """Return the ServiceCategory name for this booking, or None for legacy bookings."""
        if obj.service_category_id:
            return obj.service_category.name
        return None

    def get_category_asset_name(self, obj):
        if obj.category_asset_id:
            return obj.category_asset.name
        return None
        return None