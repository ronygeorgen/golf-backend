from rest_framework import serializers
from .models import Booking
from users.serializers import UserSerializer
from simulators.serializers import SimulatorSerializer
from coaching.serializers import CoachingPackageSerializer

class BookingCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = [
            'booking_type', 'simulator', 'duration_minutes', 
            'coaching_package', 'coach', 'start_time', 'end_time', 'total_price'
        ]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make total_price optional - it will be set automatically in validate()
        if 'total_price' in self.fields:
            self.fields['total_price'].required = False
    
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
            
            # Check for overlapping bookings
            conflicting_bookings = Booking.objects.filter(
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed']
            )
            
            if booking_type == 'simulator' and simulator:
                conflicting_bookings = conflicting_bookings.filter(
                    simulator=simulator,
                    booking_type='simulator'
                )
                if conflicting_bookings.exists():
                    raise serializers.ValidationError("This time slot is already booked for the selected simulator")
            
            if booking_type == 'coaching' and coach:
                conflicting_bookings = conflicting_bookings.filter(
                    coach=coach,
                    booking_type='coaching'
                )
                if conflicting_bookings.exists():
                    raise serializers.ValidationError("This time slot is already booked for the selected coach")
        
        # Booking-type specific validation
        if booking_type == 'coaching':
            if not coaching_package:
                raise serializers.ValidationError("A coaching package is required for coaching bookings.")
            
            session_duration = coaching_package.session_duration_minutes
            if data.get('duration_minutes') and data['duration_minutes'] != session_duration:
                raise serializers.ValidationError(
                    f"Coaching sessions must be {session_duration} minutes for the selected package."
                )
            data['duration_minutes'] = session_duration
            data['total_price'] = 0  # Session already prepaid via package
        elif booking_type == 'simulator':
            # Calculate price if not provided
            if not data.get('total_price'):
                duration = data.get('duration_minutes')
                if duration:
                    from simulators.models import DurationPrice
                    try:
                        duration_price = DurationPrice.objects.get(duration_minutes=duration)
                        data['total_price'] = duration_price.price
                    except DurationPrice.DoesNotExist:
                        data['total_price'] = 0
        
        return data

class BookingSerializer(serializers.ModelSerializer):
    client_details = UserSerializer(source='client', read_only=True)
    simulator_details = SimulatorSerializer(source='simulator', read_only=True)
    coach_details = UserSerializer(source='coach', read_only=True)
    package_details = CoachingPackageSerializer(source='coaching_package', read_only=True)
    
    class Meta:
        model = Booking
        fields = '__all__'
        read_only_fields = ['client', 'created_at', 'updated_at']