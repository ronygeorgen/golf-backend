from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from datetime import datetime, timedelta
from users.utils import get_location_id_from_request
from .models import SpecialEvent, SpecialEventRegistration
from .serializers import SpecialEventSerializer, SpecialEventRegistrationSerializer


class SpecialEventViewSet(viewsets.ModelViewSet):
    serializer_class = SpecialEventSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        from users.utils import get_location_id_from_request
        from django.db.models import Q
        location_id = get_location_id_from_request(self.request)
        queryset = SpecialEvent.objects.filter(is_active=True)
        
        # Filter by location_id (allow global events or matching location)
        if location_id:
            queryset = queryset.filter(
                Q(location_id=location_id) | 
                Q(location_id__isnull=True) | 
                Q(location_id='')
            )
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            self.request.user.role in ['admin', 'staff'] or 
            getattr(self.request.user, 'is_superuser', False)
        )
        
        # Admin and staff can see all events (including private)
        # Clients see only non-private active events with future occurrences
        if not is_admin_or_staff:
            # For clients, exclude private events and show only events that aren't finished
            today = timezone.now().date()
            queryset = queryset.filter(
                is_private=False
            ).filter(
                Q(date__gte=today) | 
                (Q(event_type__in=['weekly', 'monthly', 'yearly']) & (Q(recurring_end_date__isnull=True) | Q(recurring_end_date__gte=today)))
            )
        
        return queryset.order_by('date', 'start_time')
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        context['location_id'] = get_location_id_from_request(self.request)
        
        # For admin list view, check if we need to show upcoming or conducted events
        view_type = self.request.query_params.get('view_type', 'upcoming')  # 'upcoming' or 'conducted'
        context['view_type'] = view_type
        
        return context
    
    def list(self, request, *args, **kwargs):
        """
        List events. For admin, can filter by view_type:
        - 'upcoming': Show next upcoming occurrence for recurring events
        - 'conducted': Show all past occurrences
        """
        queryset = self.filter_queryset(self.get_queryset())
        view_type = request.query_params.get('view_type', 'upcoming')
        today = timezone.now().date()
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if is_admin_or_staff and view_type == 'upcoming':
            # For upcoming view, calculate next occurrence for each event
            result = []
            for event in queryset:
                # Auto-enroll users for next occurrence if enabled
                if event.is_auto_enroll and event.event_type in ['weekly', 'monthly']:
                    event.auto_enroll_users_for_next_occurrence()
                
                if event.event_type == 'one_time':
                    # One-time events: only show if date is in future
                    if event.date >= today:
                        serializer = self.get_serializer(event, context=self.get_serializer_context())
                        result.append(serializer.data)
                else:
                    # Recurring events: get next upcoming occurrence
                    occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
                    if occurrences:
                        next_occurrence = occurrences[0]
                        serializer = self.get_serializer(event, context={
                            **self.get_serializer_context(),
                            'occurrence_date': next_occurrence
                        })
                        data = serializer.data
                        # Update the date field to show next occurrence date
                        data['date'] = next_occurrence.strftime('%Y-%m-%d')
                        result.append(data)
            
            # Sort by occurrence date
            result.sort(key=lambda x: (x['date'], x['start_time']))
            return Response(result)
        elif is_admin_or_staff and view_type == 'conducted':
            # For conducted view, show all past occurrences
            result = []
            for event in queryset:
                if event.event_type == 'one_time':
                    # One-time events: only show if date is in past
                    if event.date < today:
                        serializer = self.get_serializer(event, context={
                            **self.get_serializer_context(),
                            'occurrence_date': event.date
                        })
                        data = serializer.data
                        result.append(data)
                else:
                    # Recurring events: get all past occurrences
                    occurrences = event.get_occurrences(
                        start_date=event.date,
                        end_date=today - timedelta(days=1)  # Up to yesterday
                    )
                    for occurrence_date in occurrences:
                        serializer = self.get_serializer(event, context={
                            **self.get_serializer_context(),
                            'occurrence_date': occurrence_date
                        })
                        data = serializer.data
                        # Update the date field to show this occurrence date
                        data['date'] = occurrence_date.strftime('%Y-%m-%d')
                        # Add event ID and occurrence date to make each row unique
                        data['display_id'] = f"{event.id}_{occurrence_date.strftime('%Y-%m-%d')}"
                        result.append(data)
            
            # Sort by date descending (most recent first)
            result.sort(key=lambda x: (x['date'], x['start_time']), reverse=True)
            return Response(result)
        else:
            # Default behavior for clients or if no view_type specified
            return super().list(request, *args, **kwargs)
    @action(detail=False, methods=['get'], url_path='calendar-events')
    def calendar_events(self, request):
        """
        Get all event occurrences within a specific date range.
        Query params: start_date, end_date (YYYY-MM-DD)
        Only accessible by admin/staff/superuser.
        """
        # Check permissions
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin_or_staff:
             return Response(
                 {'error': 'Permission denied'},
                 status=status.HTTP_403_FORBIDDEN
             )

        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        if not start_date_str or not end_date_str:
            return Response(
                {'error': 'start_date and end_date are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        location_id = get_location_id_from_request(request)
        queryset = self.get_queryset() # This already filters by location and is_active
        
        result = []
        for event in queryset:
            occurrences = event.get_occurrences(start_date=start_date, end_date=end_date)
            for occurrence_date in occurrences:
                # Use serializer for the event data
                serializer = self.get_serializer(event, context={
                    **self.get_serializer_context(),
                    'occurrence_date': occurrence_date
                })
                data = serializer.data
                
                # Override date with the specific occurrence date
                data['date'] = occurrence_date.strftime('%Y-%m-%d')
                data['display_id'] = f"{event.id}_{occurrence_date.strftime('%Y-%m-%d')}"
                
                # Add explicit start/end datetimes for calendar if needed (combined date + time)
                # Note: start_time and end_time are already in the serializer data as strings
                
                result.append(data)
        
        return Response(result)
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get all upcoming events - for recurring events, only show the first upcoming date.
        Private events are excluded for non-admin/staff users via get_queryset().
        Auto-enrolls users for events with is_auto_enroll=True."""
        today = timezone.now().date()
        events = self.get_queryset()
        
        result = []
        for event in events:
            # Auto-enroll users for next occurrence if enabled
            if event.is_auto_enroll and event.event_type in ['weekly', 'monthly']:
                event.auto_enroll_users_for_next_occurrence()
            
            occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
            if occurrences:
                next_occurrence_date = occurrences[0]
                # Pass occurrence_date to serializer context for accurate counting
                serializer = self.get_serializer(event, context={
                    **self.get_serializer_context(),
                    'occurrence_date': next_occurrence_date
                })
                data = serializer.data
                # Only show the first upcoming occurrence date
                data['next_occurrence_date'] = next_occurrence_date.strftime('%Y-%m-%d')
                result.append(data)
        
        return Response(result)
    
    @action(detail=False, methods=['get'])
    def events_on_date(self, request):
        """Get all events occurring on a specific date"""
        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'error': 'date parameter is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
            
        # Use get_queryset to respect location and ROLE (admins see everything, clients see non-private)
        # Note: If clients shouldn't see special events in the calendar at all, 
        # get_queryset will handle filtering them if we let clients access this.
        # However, calendar-events is 403 for clients, so we might want consistency here.
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        # We use filter on the base queryset but respect role for masking
        location_id = get_location_id_from_request(request)
        active_events = SpecialEvent.objects.filter(is_active=True)
        if location_id:
            active_events = active_events.filter(location_id=location_id)
            
        events_on_date = []
        for event in active_events:
            # If it's private and user is not admin/staff, skip it entirely 
            # (matches "not in calendar for normal clients")
            if event.is_private and not is_admin_or_staff:
                continue
                
            occurrences = event.get_occurrences(start_date=target_date, end_date=target_date)
            if target_date in occurrences:
                # Admins see real title, others see real title if it's not private
                # Since we skip private for non-admins above, they only see public events here.
                data = {
                    'id': event.id,
                    'title': event.title,
                    'start_time': event.start_time.strftime('%H:%M:%S'),
                    'end_time': event.end_time.strftime('%H:%M:%S'),
                    'date': target_date.strftime('%Y-%m-%d'),
                    'is_private': event.is_private,
                }
                events_on_date.append(data)
                
        return Response(events_on_date)

    @action(detail=True, methods=['post'])
    def register(self, request, pk=None):
        """Register the current user for the next occurrence of this event"""
        event = self.get_object()
        user = request.user
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        # Prevent clients from registering for private events
        if event.is_private and not is_admin_or_staff:
            return Response(
                {'error': 'This is a private event. Only admins can register clients for private events.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get the next occurrence date
        today = timezone.now().date()
        occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
        
        if not occurrences:
            return Response(
                {'error': 'No upcoming occurrences for this event'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Register for the first upcoming occurrence only
        next_occurrence_date = occurrences[0]
        
        # Check if already registered for this specific occurrence date
        existing_registration = SpecialEventRegistration.objects.filter(
            event=event,
            user=user,
            occurrence_date=next_occurrence_date,
            status__in=['registered', 'showed_up']
        ).first()
        
        if existing_registration:
            return Response(
                {'error': 'You are already registered for this event occurrence'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if event is full for this specific occurrence date
        # Count both registered and showed_up statuses
        total_registrations = SpecialEventRegistration.objects.filter(
            event=event,
            occurrence_date=next_occurrence_date,
            status__in=['registered', 'showed_up']
        ).count()
        
        if total_registrations >= event.max_capacity:
            return Response(
                {'error': 'This event occurrence is full'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Use update_or_create to handle case where user previously cancelled for this occurrence
        # This prevents IntegrityError when re-registering after cancellation
        registration, created = SpecialEventRegistration.objects.update_or_create(
            event=event,
            user=user,
            occurrence_date=next_occurrence_date,
            defaults={
                'status': 'registered'
            }
        )
        
        # If re-registering after cancellation, update the registered_at timestamp
        if not created and registration.status == 'registered':
            registration.registered_at = timezone.now()
            registration.save(update_fields=['registered_at'])
        
        serializer = SpecialEventRegistrationSerializer(registration)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'])
    def cancel_registration(self, request, pk=None):
        """Cancel the current user's registration for the next occurrence of this event"""
        event = self.get_object()
        user = request.user
        
        # Get the next occurrence date
        today = timezone.now().date()
        occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
        
        if not occurrences:
            return Response(
                {'error': 'No upcoming occurrences for this event'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        next_occurrence_date = occurrences[0]
        
        registration = SpecialEventRegistration.objects.filter(
            event=event,
            user=user,
            occurrence_date=next_occurrence_date,
            status='registered'
        ).first()
        
        if not registration:
            return Response(
                {'error': 'You are not registered for this event occurrence'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        registration.status = 'cancelled'
        registration.save()
        
        serializer = SpecialEventRegistrationSerializer(registration)
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        """Set location_id when creating event"""
        location_id = get_location_id_from_request(self.request)
        if location_id:
            serializer.save(location_id=location_id)
        else:
            serializer.save()
    
    @action(detail=True, methods=['get'])
    def registrations(self, request, pk=None):
        """Get all registrations for this event (admin/staff only) - show all registrations including cancelled"""
        event = self.get_object()
        
        # Verify event belongs to admin/staff's location
        location_id = get_location_id_from_request(request)
        if location_id and event.location_id != location_id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You can only view registrations for events in your location.")
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin_or_staff:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Optional filter by occurrence_date if provided
        occurrence_date = request.query_params.get('occurrence_date')
        
        # Show all registrations (including admin/staff and cancelled) for admin view
        registrations = SpecialEventRegistration.objects.filter(
            event=event
        )
        
        if occurrence_date:
            try:
                from datetime import datetime
                occurrence_date_obj = datetime.strptime(occurrence_date, '%Y-%m-%d').date()
                registrations = registrations.filter(occurrence_date=occurrence_date_obj)
            except ValueError:
                pass  # Invalid date format, ignore filter
        
        registrations = registrations.select_related('user').order_by('-registered_at')
        
        serializer = SpecialEventRegistrationSerializer(registrations, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['patch'])
    def update_registration_status(self, request, pk=None):
        """Update a user's registration status (admin/staff only)"""
        event = self.get_object()
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin_or_staff:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        registration_id = request.data.get('registration_id')
        new_status = request.data.get('status')
        
        if not registration_id or not new_status:
            return Response(
                {'error': 'registration_id and status are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if new_status not in ['registered', 'showed_up', 'cancelled']:
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            registration = SpecialEventRegistration.objects.get(
                id=registration_id,
                event=event
            )
            registration.status = new_status
            registration.save()
            
            serializer = SpecialEventRegistrationSerializer(registration)
            return Response(serializer.data)
        except SpecialEventRegistration.DoesNotExist:
            return Response(
                {'error': 'Registration not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['delete'])
    def remove_registration(self, request, pk=None):
        """Remove a registration from an event (admin/staff only)"""
        event = self.get_object()
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin_or_staff:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        registration_id = request.data.get('registration_id')
        
        if not registration_id:
            return Response(
                {'error': 'registration_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            registration = SpecialEventRegistration.objects.get(
                id=registration_id,
                event=event
            )
            registration.delete()
            
            return Response(
                {'message': 'Registration removed successfully'},
                status=status.HTTP_200_OK
            )
        except SpecialEventRegistration.DoesNotExist:
            return Response(
                {'error': 'Registration not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['post'])
    def register_user(self, request, pk=None):
        """Register a user for an event (admin/staff only)"""
        event = self.get_object()
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin_or_staff:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        user_id = request.data.get('user_id')
        occurrence_date_str = request.data.get('occurrence_date')
        
        if not user_id:
            return Response(
                {'error': 'user_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get the user to register
        from users.models import User
        try:
            user_to_register = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Determine occurrence date
        today = timezone.now().date()
        occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
        
        if not occurrences:
            return Response(
                {'error': 'No upcoming occurrences for this event'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Use provided occurrence_date or default to first upcoming occurrence
        if occurrence_date_str:
            try:
                occurrence_date = datetime.strptime(occurrence_date_str, '%Y-%m-%d').date()
                # Validate that this date is a valid occurrence
                if occurrence_date not in occurrences:
                    return Response(
                        {'error': 'Invalid occurrence date for this event'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            occurrence_date = occurrences[0]
        
        # Check if already registered for this specific occurrence date
        existing_registration = SpecialEventRegistration.objects.filter(
            event=event,
            user=user_to_register,
            occurrence_date=occurrence_date,
            status__in=['registered', 'showed_up']
        ).first()
        
        if existing_registration:
            return Response(
                {'error': 'User is already registered for this event occurrence'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if event is full for this specific occurrence date
        total_registrations = event.get_registered_count(occurrence_date)
        
        if total_registrations >= event.max_capacity:
            return Response(
                {'error': 'This event occurrence is full'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create registration
        registration, created = SpecialEventRegistration.objects.update_or_create(
            event=event,
            user=user_to_register,
            occurrence_date=occurrence_date,
            defaults={
                'status': 'registered'
            }
        )
        
        # If re-registering after cancellation, update the registered_at timestamp
        if not created and registration.status == 'registered':
            registration.registered_at = timezone.now()
            registration.save(update_fields=['registered_at'])
        
        serializer = SpecialEventRegistrationSerializer(registration)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def future_occurrences(self, request, pk=None):
        """Get all future occurrences of this event"""
        event = self.get_object()
        today = timezone.now().date()
        # Get occurrences for next 2 years to be safe
        occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365*2))
        return Response([d.strftime('%Y-%m-%d') for d in occurrences])

    @action(detail=True, methods=['post'])
    def pause_occurrences(self, request, pk=None):
        """Pause specific occurrences of an event and optionally extend the end date"""
        event = self.get_object()
        dates_to_pause = request.data.get('dates', [])
        should_extend = request.data.get('extend', False)
        
        if not dates_to_pause:
            return Response({'error': 'No dates provided'}, status=status.HTTP_400_BAD_REQUEST)
            
        paused_count = 0
        
        # Create paused dates
        from .models import SpecialEventPausedDate
        
        for date_str in dates_to_pause:
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                # Create if not exists
                if not SpecialEventPausedDate.objects.filter(event=event, date=date_obj).exists():
                     SpecialEventPausedDate.objects.create(event=event, date=date_obj)
                     paused_count += 1
            except ValueError:
                continue
                
        if should_extend and paused_count > 0 and event.recurring_end_date:
            # Extend the recurring_end_date
            if event.event_type == 'weekly':
                event.recurring_end_date += timedelta(weeks=paused_count)
            elif event.event_type == 'monthly':
                # Add exact number of months
                new_date = event.recurring_end_date
                months_to_add = paused_count
                
                # Calculate new year and month
                # Month is 1-12, so subtract 1 for 0-indexed calculation, then add back
                new_month_index = (new_date.month - 1) + months_to_add
                year_add = new_month_index // 12
                new_year = new_date.year + year_add
                new_month = (new_month_index % 12) + 1
                
                # Handle day overflow (e.g. proceeding from Jan 31 to Feb)
                import calendar
                day = min(new_date.day, calendar.monthrange(new_year, new_month)[1])
                
                event.recurring_end_date = new_date.replace(year=new_year, month=new_month, day=day)
                
            event.save()
            
        return Response({
            'message': 'Occurrences paused successfully', 
            'extended': should_extend, 
            'paused_count': paused_count,
            'new_end_date': event.recurring_end_date if should_extend and event.recurring_end_date else None
        })


class SpecialEventRegistrationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SpecialEventRegistrationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # Users can see their own registrations
        # Admin/staff can see all registrations
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            self.request.user.role in ['admin', 'staff'] or 
            getattr(self.request.user, 'is_superuser', False)
        )
        
        if is_admin_or_staff:
            return SpecialEventRegistration.objects.all().select_related('user', 'event')
        else:
            return SpecialEventRegistration.objects.filter(
                user=self.request.user
            ).select_related('user', 'event')
