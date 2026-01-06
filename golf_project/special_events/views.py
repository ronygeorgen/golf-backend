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
        location_id = get_location_id_from_request(self.request)
        queryset = SpecialEvent.objects.filter(is_active=True)
        
        # Filter by location_id
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        
        # Check if user is admin (including superuser)
        is_admin = (
            self.request.user.role == 'admin' or 
            getattr(self.request.user, 'is_superuser', False)
        )
        
        # Only admins can see private events
        # Staff and clients see only non-private events
        if not is_admin:
            # For staff and clients, exclude private events
            queryset = queryset.filter(is_private=False)
            
            # For clients, also filter to show only future events
            if self.request.user.role not in ['admin', 'staff']:
                today = timezone.now().date()
                queryset = queryset.filter(date__gte=today)
        
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
        
        # Check if user is admin (including superuser)
        is_admin = (
            request.user.role == 'admin' or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if is_admin and view_type == 'upcoming':
            # For upcoming view, calculate next occurrence for each event
            result = []
            for event in queryset:
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
        elif is_admin and view_type == 'conducted':
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
    
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get all upcoming events - for recurring events, only show the first upcoming date.
        Private events are excluded for non-admin/staff users."""
        today = timezone.now().date()
        events = self.get_queryset()  # This already filters out private events for clients
        
        result = []
        for event in events:
            occurrences = event.get_occurrences(start_date=today, end_date=today + timedelta(days=365))
            if occurrences:
                next_occurrence_date = occurrences[0]
                # Pass occurrence_date to serializer context for accurate counting
                serializer = self.get_serializer(event, context={
                    'request': request,
                    'occurrence_date': next_occurrence_date
                })
                data = serializer.data
                # Only show the first upcoming occurrence date
                data['next_occurrence_date'] = next_occurrence_date.strftime('%Y-%m-%d')
                result.append(data)
        
        return Response(result)
    
    @action(detail=True, methods=['post'])
    def register(self, request, pk=None):
        """Register the current user for the next occurrence of this event"""
        event = self.get_object()
        user = request.user
        
        # Check if user is admin (including superuser)
        is_admin = (
            request.user.role == 'admin' or 
            getattr(request.user, 'is_superuser', False)
        )
        
        # Prevent staff and clients from registering for private events
        if event.is_private and not is_admin:
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
        """Get all registrations for this event (admin only) - show all registrations including cancelled"""
        event = self.get_object()
        
        # Verify event belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and event.location_id != location_id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You can only view registrations for events in your location.")
        
        # Check if user is admin (including superuser)
        is_admin = (
            request.user.role == 'admin' or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin:
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
        """Update a user's registration status (admin only)"""
        event = self.get_object()
        
        # Check if user is admin (including superuser)
        is_admin = (
            request.user.role == 'admin' or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin:
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
    
    @action(detail=True, methods=['post'])
    def register_user(self, request, pk=None):
        """Register a user for an event (admin only)"""
        event = self.get_object()
        
        # Check if user is admin (including superuser)
        is_admin = (
            request.user.role == 'admin' or 
            getattr(request.user, 'is_superuser', False)
        )
        
        if not is_admin:
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


class SpecialEventRegistrationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SpecialEventRegistrationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # Users can see their own registrations
        # Only admins can see all registrations
        # Check if user is admin (including superuser)
        is_admin = (
            self.request.user.role == 'admin' or 
            getattr(self.request.user, 'is_superuser', False)
        )
        
        if is_admin:
            return SpecialEventRegistration.objects.all().select_related('user', 'event')
        else:
            return SpecialEventRegistration.objects.filter(
                user=self.request.user
            ).select_related('user', 'event')
