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
    
    def validate(self, data):
        # Check for booking conflicts
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        simulator = data.get('simulator')
        coach = data.get('coach')
        booking_type = data.get('booking_type')
        
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
        
        # Calculate price if not provided
        if not data.get('total_price'):
            if booking_type == 'simulator':
                # Get price from DurationPrice
                duration = data.get('duration_minutes')
                if duration:
                    from simulators.models import DurationPrice
                    try:
                        duration_price = DurationPrice.objects.get(duration_minutes=duration)
                        data['total_price'] = duration_price.price
                    except DurationPrice.DoesNotExist:
                        # Default price if not found
                        data['total_price'] = 0
            elif booking_type == 'coaching':
                # Get price from coaching package
                package = data.get('coaching_package')
                if package:
                    data['total_price'] = package.price
                else:
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