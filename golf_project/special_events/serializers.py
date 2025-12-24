from rest_framework import serializers
from datetime import datetime
from .models import SpecialEvent, SpecialEventRegistration
from users.serializers import UserSerializer


class SpecialEventSerializer(serializers.ModelSerializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    date = serializers.DateField()
    registered_count = serializers.SerializerMethodField()
    showed_up_count = serializers.SerializerMethodField()
    available_spots = serializers.SerializerMethodField()
    is_full = serializers.SerializerMethodField()
    user_registered = serializers.SerializerMethodField()
    user_registration_status = serializers.SerializerMethodField()
    next_occurrence_date = serializers.SerializerMethodField()
    
    class Meta:
        model = SpecialEvent
        fields = [
            'id', 'title', 'description', 'event_type', 'date', 'recurring_end_date',
            'start_time', 'end_time', 'max_capacity', 'is_active',
            'price', 'show_price', 'location_id',
            'registered_count', 'showed_up_count', 'available_spots',
            'is_full', 'user_registered', 'user_registration_status',
            'next_occurrence_date', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']
    
    def get_registered_count(self, obj):
        # Get the occurrence date from context if available (for upcoming events)
        occurrence_date = self.context.get('occurrence_date')
        return obj.get_registered_count(occurrence_date)
    
    def get_showed_up_count(self, obj):
        occurrence_date = self.context.get('occurrence_date')
        return obj.get_showed_up_count(occurrence_date)
    
    def get_available_spots(self, obj):
        occurrence_date = self.context.get('occurrence_date')
        return obj.get_available_spots(occurrence_date)
    
    def get_is_full(self, obj):
        occurrence_date = self.context.get('occurrence_date')
        return obj.is_full(occurrence_date)
    
    def get_user_registered(self, obj):
        request = self.context.get('request')
        occurrence_date = self.context.get('occurrence_date')
        if request and request.user and request.user.is_authenticated:
            query = SpecialEventRegistration.objects.filter(
                event=obj,
                user=request.user,
                status__in=['registered', 'showed_up']
            )
            if occurrence_date:
                query = query.filter(occurrence_date=occurrence_date)
            return query.exists()
        return False
    
    def get_user_registration_status(self, obj):
        request = self.context.get('request')
        occurrence_date = self.context.get('occurrence_date')
        if request and request.user and request.user.is_authenticated:
            query = SpecialEventRegistration.objects.filter(
                event=obj,
                user=request.user
            )
            if occurrence_date:
                query = query.filter(occurrence_date=occurrence_date)
            registration = query.first()
            if registration:
                return registration.status
        return None
    
    def get_next_occurrence_date(self, obj):
        # Return the occurrence date from context if available
        occurrence_date = self.context.get('occurrence_date')
        if occurrence_date:
            if isinstance(occurrence_date, str):
                return occurrence_date
            return occurrence_date.strftime('%Y-%m-%d')
        # Otherwise calculate next occurrence
        from django.utils import timezone
        from datetime import timedelta
        today = timezone.now().date()
        occurrences = obj.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
        if occurrences:
            return occurrences[0].strftime('%Y-%m-%d')
        return None
    
    def to_representation(self, instance):
        """Return UTC times as-is (no conversion)"""
        representation = super().to_representation(instance)
        # Format times as HH:MM for consistency
        if representation.get('start_time'):
            if isinstance(representation['start_time'], str):
                if len(representation['start_time'].split(':')) > 2:
                    representation['start_time'] = representation['start_time'][:5]
            else:
                representation['start_time'] = representation['start_time'].strftime('%H:%M')
        if representation.get('end_time'):
            if isinstance(representation['end_time'], str):
                if len(representation['end_time'].split(':')) > 2:
                    representation['end_time'] = representation['end_time'][:5]
            else:
                representation['end_time'] = representation['end_time'].strftime('%H:%M')
        # Format date as YYYY-MM-DD
        if representation.get('date'):
            if isinstance(representation['date'], str):
                representation['date'] = representation['date']
            else:
                representation['date'] = representation['date'].strftime('%Y-%m-%d')
        # Format recurring_end_date as YYYY-MM-DD
        if representation.get('recurring_end_date'):
            if isinstance(representation['recurring_end_date'], str):
                representation['recurring_end_date'] = representation['recurring_end_date']
            else:
                representation['recurring_end_date'] = representation['recurring_end_date'].strftime('%Y-%m-%d')
        # Format next_occurrence_date if present
        if representation.get('next_occurrence_date'):
            if isinstance(representation['next_occurrence_date'], str):
                representation['next_occurrence_date'] = representation['next_occurrence_date']
            else:
                representation['next_occurrence_date'] = representation['next_occurrence_date'].strftime('%Y-%m-%d')
        return representation
    
    def to_internal_value(self, data):
        """Accept UTC times as-is (no conversion)"""
        if 'start_time' in data and data['start_time']:
            start_time_str = data['start_time']
            if isinstance(start_time_str, str):
                try:
                    time_obj = datetime.strptime(start_time_str, '%H:%M').time()
                    data['start_time'] = time_obj
                except ValueError:
                    pass
        if 'end_time' in data and data['end_time']:
            end_time_str = data['end_time']
            if isinstance(end_time_str, str):
                try:
                    time_obj = datetime.strptime(end_time_str, '%H:%M').time()
                    data['end_time'] = time_obj
                except ValueError:
                    pass
        return super().to_internal_value(data)
    
    def validate(self, attrs):
        """Check if the event date/time is on a closed day and validate recurring_end_date"""
        date = attrs.get('date')
        recurring_end_date = attrs.get('recurring_end_date')
        event_type = attrs.get('event_type', self.instance.event_type if self.instance else 'one_time')
        start_time = attrs.get('start_time')
        
        # Validate recurring_end_date for recurring events
        if event_type != 'one_time' and recurring_end_date:
            if date and recurring_end_date < date:
                raise serializers.ValidationError({
                    'recurring_end_date': "Recurring end date must be on or after the start date."
                })
        
        if date and start_time:
            from admin_panel.models import ClosedDay
            from django.utils import timezone
            from datetime import datetime as dt
            
            # Create datetime for checking
            check_datetime = timezone.make_aware(dt.combine(date, start_time))
            location_id = self.context.get('location_id') if hasattr(self, 'context') else None
            is_closed, message = ClosedDay.check_if_closed(check_datetime, location_id=location_id)
            
            if is_closed:
                raise serializers.ValidationError({
                    'date': message or "Facility is closed on this date/time. Special events cannot be created on closed days."
                })
        
        return attrs


class SpecialEventRegistrationSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    event_details = SpecialEventSerializer(source='event', read_only=True)
    occurrence_date = serializers.DateField()
    
    class Meta:
        model = SpecialEventRegistration
        fields = [
            'id', 'event', 'user', 'occurrence_date', 'status', 'registered_at', 'updated_at',
            'user_details', 'event_details'
        ]
        read_only_fields = ['registered_at', 'updated_at']
    
    def to_representation(self, instance):
        """Format date as YYYY-MM-DD for consistency"""
        representation = super().to_representation(instance)
        # Format occurrence_date as YYYY-MM-DD
        if representation.get('occurrence_date'):
            if isinstance(representation['occurrence_date'], str):
                representation['occurrence_date'] = representation['occurrence_date']
            else:
                representation['occurrence_date'] = representation['occurrence_date'].strftime('%Y-%m-%d')
        return representation
    
    def to_internal_value(self, data):
        """Accept date as YYYY-MM-DD string"""
        if 'occurrence_date' in data and data['occurrence_date']:
            occurrence_date_str = data['occurrence_date']
            if isinstance(occurrence_date_str, str):
                try:
                    from datetime import datetime
                    date_obj = datetime.strptime(occurrence_date_str, '%Y-%m-%d').date()
                    data['occurrence_date'] = date_obj
                except ValueError:
                    pass
        return super().to_internal_value(data)

