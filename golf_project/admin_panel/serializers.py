from rest_framework import serializers
from decimal import Decimal
from coaching.models import CoachingPackagePurchase
from simulators.models import SimulatorCredit
from users.models import User
from .models import ClosedDay


class ClientLookupMixin:
    def _resolve_client(self, attrs):
        client = None
        client_id = attrs.get('client_id')
        identifier = attrs.get('client_identifier')
        
        if client_id:
            client = User.objects.filter(id=client_id).first()
        if not client and identifier:
            lookup = identifier.strip()
            client = User.objects.filter(email__iexact=lookup).first()
            if not client and lookup.isdigit():
                client = User.objects.filter(phone=lookup).first()
        if not client:
            raise serializers.ValidationError("Client not found. Provide a valid client_id or client_identifier (email/phone).")
        attrs['client'] = client
        return attrs


class CoachingSessionAdjustmentSerializer(serializers.Serializer, ClientLookupMixin):
    client_id = serializers.IntegerField(required=False)
    client_identifier = serializers.CharField(required=False, allow_blank=True)
    package_purchase_id = serializers.IntegerField(required=False)
    package_id = serializers.IntegerField(required=False)
    session_count = serializers.IntegerField(min_value=1, default=1)
    simulator_hours = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal('0'), required=False, default=Decimal('0'))
    note = serializers.CharField(required=False, allow_blank=True)
    create_if_missing = serializers.BooleanField(required=False, default=False)
    
    def validate(self, attrs):
        attrs = self._resolve_client(attrs)
        client = attrs['client']
        purchase = None
        purchase_id = attrs.get('package_purchase_id')
        package_id = attrs.get('package_id')
        create_if_missing = attrs.get('create_if_missing', False)
        
        if purchase_id:
            purchase = CoachingPackagePurchase.objects.filter(id=purchase_id, client=client).first()
            if not purchase:
                raise serializers.ValidationError("Package purchase not found for this client.")
        elif package_id:
            purchase = CoachingPackagePurchase.objects.filter(
                client=client,
                package_id=package_id
            ).exclude(package_status='completed').order_by('-purchased_at').first()
            
            if not purchase:
                if create_if_missing:
                    from coaching.models import CoachingPackage
                    try:
                        package = CoachingPackage.objects.get(id=package_id)
                        attrs['selected_package'] = package
                    except CoachingPackage.DoesNotExist:
                        raise serializers.ValidationError("Selected package does not exist.")
                else:
                    raise serializers.ValidationError(
                        "The client does not have an active purchase for the selected package.",
                        code='no_active_purchase'
                    )
        else:
            purchase = CoachingPackagePurchase.objects.filter(
                client=client,
                package_status='active'
            ).order_by('-purchased_at').first()
            
            if not purchase:
                if package_id and create_if_missing:
                     # This block allows falling back to creation if no generic active purchase is found but package_id provided 
                     # (Though logic flow makes this unreachable if package_id WAS provided, as it hits the elif above)
                     pass
                else:
                     raise serializers.ValidationError("No active purchases found for this client. Provide a package_id or purchase reference.")
        
        attrs['purchase'] = purchase
        return attrs


class SimulatorCreditGrantSerializer(serializers.Serializer, ClientLookupMixin):
    client_id = serializers.IntegerField(required=False)
    client_identifier = serializers.CharField(required=False, allow_blank=True)
    hours = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal('0.01'), required=False)
    token_count = serializers.IntegerField(min_value=1, required=False)  # Deprecated, kept for backward compatibility
    reason = serializers.ChoiceField(choices=SimulatorCredit.Reason.choices, default=SimulatorCredit.Reason.MANUAL)
    note = serializers.CharField(required=False, allow_blank=True)
    
    def validate(self, attrs):
        attrs = self._resolve_client(attrs)
        # If hours not provided, use token_count (backward compatibility) or default to 1 hour
        if 'hours' not in attrs or attrs['hours'] is None:
            if 'token_count' in attrs and attrs['token_count'] is not None:
                attrs['hours'] = float(attrs['token_count'])  # Convert count to hours for backward compatibility
            else:
                attrs['hours'] = 1.0  # Default to 1 hour
        return attrs


class ClosedDaySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClosedDay
        fields = [
            'id',
            'title',
            'description',
            'start_date',
            'end_date',
            'start_time',
            'end_time',
            'recurrence',
            'is_active',
            'location_id',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']
    
    def validate(self, attrs):
        from datetime import datetime, timedelta, date as date_obj
        from django.utils import timezone
        from users.models import StaffDayAvailability, StaffAvailability
        from special_events.models import SpecialEvent
        from bookings.models import Booking
        
        start_date = attrs.get('start_date')
        end_date = attrs.get('end_date')
        start_time = attrs.get('start_time')
        end_time = attrs.get('end_time')
        instance = self.instance  # For updates, exclude current instance
        
        if start_date and end_date:
            if end_date < start_date:
                raise serializers.ValidationError({
                    'end_date': 'End date cannot be before start date.'
                })
        
        if start_time and end_time:
            if end_time <= start_time:
                raise serializers.ValidationError({
                    'end_time': 'End time must be after start time.'
                })
        
        # Check for conflicts only if creating a new closed day or if dates changed
        if start_date and end_date:
            conflicts = []
            current_date = start_date
            
            # Check each date in the range
            while current_date <= end_date:
                # Check staff day-specific availability
                staff_day_availabilities = StaffDayAvailability.objects.filter(date=current_date)
                if staff_day_availabilities.exists():
                    staff_names = [f"{avail.staff.first_name} {avail.staff.last_name}" for avail in staff_day_availabilities[:5]]
                    count = staff_day_availabilities.count()
                    if count > 5:
                        conflicts.append(f"• {count} staff members have availability set on {current_date.strftime('%Y-%m-%d')} (e.g., {', '.join(staff_names)})")
                    else:
                        conflicts.append(f"• Staff members have availability set on {current_date.strftime('%Y-%m-%d')}: {', '.join(staff_names)}")
                
                # Check staff weekly availability (for one_time, check if day of week matches weekly availability)
                recurrence = attrs.get('recurrence', 'one_time')
                if recurrence == 'one_time':
                    day_of_week = current_date.weekday()
                    weekly_availabilities = StaffAvailability.objects.filter(day_of_week=day_of_week)
                    if weekly_availabilities.exists():
                        staff_names = [f"{avail.staff.first_name} {avail.staff.last_name}" for avail in weekly_availabilities[:5]]
                        count = weekly_availabilities.count()
                        day_name = current_date.strftime('%A')
                        if count > 5:
                            conflicts.append(f"• {count} staff members have weekly availability on {day_name} (e.g., {', '.join(staff_names)})")
                        else:
                            conflicts.append(f"• Staff members have weekly availability on {day_name}: {', '.join(staff_names)}")
                elif recurrence == 'weekly':
                    # For weekly recurring, check if there are any bookings/events on future occurrences
                    day_of_week = current_date.weekday()  # 0=Monday, 6=Sunday
                    # Django week_day: 1=Sunday, 2=Monday, ..., 7=Saturday
                    # Convert: weekday 0 (Mon) -> week_day 2, weekday 6 (Sun) -> week_day 1
                    django_week_day = (day_of_week + 2) % 7
                    if django_week_day == 0:
                        django_week_day = 7
                    
                    # Check staff weekly availability
                    weekly_availabilities = StaffAvailability.objects.filter(day_of_week=day_of_week)
                    if weekly_availabilities.exists():
                        staff_names = [f"{avail.staff.first_name} {avail.staff.last_name}" for avail in weekly_availabilities[:5]]
                        count = weekly_availabilities.count()
                        day_name = current_date.strftime('%A')
                        if count > 5:
                            conflicts.append(f"• {count} staff members have weekly availability on {day_name} (e.g., {', '.join(staff_names)})")
                        else:
                            conflicts.append(f"• Staff members have weekly availability on {day_name}: {', '.join(staff_names)}")
                    
                    # Check bookings on future occurrences (next 52 weeks)
                    from django.utils import timezone
                    today = timezone.now().date()
                    future_bookings = Booking.objects.filter(
                        start_time__date__gte=today,
                        start_time__date__week_day=django_week_day,
                        status__in=['confirmed', 'completed']
                    )
                    if future_bookings.exists():
                        booking_count = future_bookings.count()
                        day_name = current_date.strftime('%A')
                        conflicts.append(f"• {booking_count} booking(s) exist on future {day_name}s (weekly recurring would conflict)")
                    
                    # Check special events on future occurrences (one-time and recurring)
                    # Check one-time events on future occurrences
                    future_events = SpecialEvent.objects.filter(
                        is_active=True,
                        date__gte=today,
                        date__week_day=django_week_day
                    )
                    # Check weekly recurring events
                    weekly_recurring_events = SpecialEvent.objects.filter(
                        is_active=True,
                        event_type='weekly',
                        date__lte=current_date  # Event started before or on this date
                    )
                    # Check if weekly recurring event would occur on this day of week
                    for event in weekly_recurring_events:
                        if event.date.weekday() == day_of_week:
                            future_events = future_events | SpecialEvent.objects.filter(id=event.id)
                    
                    if future_events.exists():
                        event_titles = [event.title for event in future_events[:5]]
                        count = future_events.count()
                        day_name = current_date.strftime('%A')
                        if count > 5:
                            conflicts.append(f"• {count} special events scheduled on future {day_name}s (e.g., {', '.join(event_titles)})")
                        else:
                            conflicts.append(f"• Special events scheduled on future {day_name}s: {', '.join(event_titles)}")
                elif recurrence == 'yearly':
                    # For yearly recurring, check if there are any bookings/events on future occurrences
                    month = current_date.month
                    day = current_date.day
                    today = timezone.now().date()
                    # Check bookings on future occurrences (next 10 years)
                    future_bookings = Booking.objects.filter(
                        start_time__date__gte=today,
                        start_time__date__month=month,
                        start_time__date__day=day,
                        status__in=['confirmed', 'completed']
                    )
                    if future_bookings.exists():
                        booking_count = future_bookings.count()
                        conflicts.append(f"• {booking_count} booking(s) exist on future occurrences of {current_date.strftime('%B %d')} (yearly recurring would conflict)")
                    
                    # Check special events on future occurrences (one-time and recurring)
                    # Check one-time events on future occurrences
                    future_events = SpecialEvent.objects.filter(
                        is_active=True,
                        date__gte=today,
                        date__month=month,
                        date__day=day
                    )
                    # Check yearly recurring events
                    yearly_recurring_events = SpecialEvent.objects.filter(
                        is_active=True,
                        event_type='yearly',
                        date__month=month,
                        date__day=day
                    )
                    future_events = future_events | yearly_recurring_events
                    
                    if future_events.exists():
                        event_titles = [event.title for event in future_events[:5]]
                        count = future_events.count()
                        if count > 5:
                            conflicts.append(f"• {count} special events scheduled on future occurrences of {current_date.strftime('%B %d')} (e.g., {', '.join(event_titles)})")
                        else:
                            conflicts.append(f"• Special events scheduled on future occurrences of {current_date.strftime('%B %d')}: {', '.join(event_titles)}")
                
                # Check special events
                special_events = SpecialEvent.objects.filter(
                    is_active=True,
                    date=current_date
                )
                if special_events.exists():
                    event_titles = [event.title for event in special_events[:5]]
                    count = special_events.count()
                    if count > 5:
                        conflicts.append(f"• {count} special events scheduled on {current_date.strftime('%Y-%m-%d')} (e.g., {', '.join(event_titles)})")
                    else:
                        conflicts.append(f"• Special events scheduled on {current_date.strftime('%Y-%m-%d')}: {', '.join(event_titles)}")
                
                # Check bookings (simulator and coaching)
                bookings = Booking.objects.filter(
                    start_time__date=current_date,
                    status__in=['confirmed', 'completed']
                )
                if bookings.exists():
                    booking_count = bookings.count()
                    simulator_count = bookings.filter(booking_type='simulator').count()
                    coaching_count = bookings.filter(booking_type='coaching').count()
                    conflicts.append(f"• {booking_count} booking(s) on {current_date.strftime('%Y-%m-%d')} ({simulator_count} simulator, {coaching_count} coaching)")
                
                current_date += timedelta(days=1)
            
            # If there are conflicts, check for force override
            if conflicts:
                force_override = self.context.get('request').data.get('force_override', False)
                
                if not force_override:
                    conflict_message = "Cannot create closed day due to existing conflicts:\n\n" + "\n".join(conflicts)
                    conflict_message += "\n\nPlease remove or reschedule these items before creating a closed day."
                    raise serializers.ValidationError({
                        'start_date': conflict_message,
                        'conflicts': True  # Custom flag for frontend to detect this specific error
                    })
        
        return attrs

