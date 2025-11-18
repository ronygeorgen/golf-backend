from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from django.db.models import Q, Sum
from django.utils import timezone
from datetime import datetime, timedelta
from .models import Booking
from .serializers import BookingSerializer, BookingCreateSerializer
from users.models import User
from simulators.models import Simulator

class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        
        # Admins and staff can see all bookings
        if user.role in ['admin', 'staff']:
            queryset = Booking.objects.all()
        else:
            # Clients can only see their own bookings
            queryset = Booking.objects.filter(client=user)
        
        # Apply filters
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        booking_type = self.request.query_params.get('booking_type')
        if booking_type:
            queryset = queryset.filter(booking_type=booking_type)
        
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(
                start_time__date__gte=start_date,
                start_time__date__lte=end_date
            )
        
        # For admin dashboard - recent bookings
        recent = self.request.query_params.get('recent')
        if recent:
            queryset = queryset.order_by('-created_at')[:10]
        
        return queryset.select_related(
            'client', 'simulator', 'coach', 'coaching_package'
    ).prefetch_related()
    
    def get_serializer_class(self):
        if self.action in ['create', 'update']:
            return BookingCreateSerializer
        return BookingSerializer
    
    def perform_create(self, serializer):
        # Set the client to the current user
        booking_data = serializer.validated_data
        booking_type = booking_data.get('booking_type')
        
        # For simulator bookings, assign simulator if not provided
        if booking_type == 'simulator' and not booking_data.get('simulator'):
            # Find first available simulator for the time slot
            start_time = booking_data.get('start_time')
            end_time = booking_data.get('end_time')
            duration = booking_data.get('duration_minutes', 60)
            
            if start_time and end_time:
                # Check for available simulators
                conflicting_bookings = Booking.objects.filter(
                    start_time__lt=end_time,
                    end_time__gt=start_time,
                    status__in=['confirmed', 'completed'],
                    booking_type='simulator'
                )
                booked_simulator_ids = conflicting_bookings.values_list('simulator_id', flat=True)
                
                available_simulator = Simulator.objects.filter(
                    is_active=True,
                    is_coaching_bay=False
                ).exclude(id__in=booked_simulator_ids).first()
                
                if available_simulator:
                    serializer.save(client=self.request.user, simulator=available_simulator)
                else:
                    raise serializers.ValidationError("No simulators available for this time slot")
            else:
                serializer.save(client=self.request.user)
        else:
            serializer.save(client=self.request.user)
    
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get all upcoming bookings for the current user"""
        upcoming_bookings = Booking.objects.filter(
            client=request.user,
            start_time__gte=timezone.now()
        ).exclude(status='cancelled').order_by('start_time')
        
        serializer = self.get_serializer(upcoming_bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def today(self, request):
        """Get today's bookings (for admin/staff)"""
        if request.user.role not in ['admin', 'staff']:
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        today = timezone.now().date()
        today_bookings = Booking.objects.filter(
            start_time__date=today
        ).order_by('start_time')
        
        serializer = self.get_serializer(today_bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """Update booking status"""
        booking = self.get_object()
        new_status = request.data.get('status')
        
        if not new_status:
            return Response(
                {'error': 'Status is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        valid_statuses = [choice[0] for choice in Booking.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response(
                {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        booking.status = new_status
        booking.save()
        
        # Log status change
        print(f"Booking {booking.id} status changed to {new_status} by {request.user}")
        
        return Response({
            'message': f'Booking status updated to {new_status}',
            'status': new_status
        })
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a booking"""
        booking = self.get_object()
        
        # Check if user has permission to cancel this booking
        if booking.client != request.user and request.user.role not in ['admin', 'staff']:
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        booking.status = 'cancelled'
        booking.save()
        
        return Response({
            'message': 'Booking cancelled successfully'
        })
    
    @action(detail=False, methods=['get'])
    def calendar_events(self, request):
        """Get bookings for calendar view"""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        booking_type = request.query_params.get('booking_type')  # 'simulator' or 'coaching'
        
        if not start_date or not end_date:
            return Response(
                {'error': 'start_date and end_date are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Handle ISO format with 'Z' or timezone offset
            if start_date.endswith('Z'):
                start_date = start_date.replace('Z', '+00:00')
            if end_date.endswith('Z'):
                end_date = end_date.replace('Z', '+00:00')
            
            # Parse ISO format datetime
            if 'T' in start_date:
                start_datetime = datetime.fromisoformat(start_date)
            else:
                # If just date, assume start of day
                start_datetime = datetime.fromisoformat(f"{start_date}T00:00:00+00:00")
            
            if 'T' in end_date:
                end_datetime = datetime.fromisoformat(end_date)
            else:
                # If just date, assume end of day
                end_datetime = datetime.fromisoformat(f"{end_date}T23:59:59+00:00")
            
            # Make timezone aware if not already
            if timezone.is_naive(start_datetime):
                start_datetime = timezone.make_aware(start_datetime)
            if timezone.is_naive(end_datetime):
                end_datetime = timezone.make_aware(end_datetime)
                
        except (ValueError, AttributeError) as e:
            return Response(
                {'error': f'Invalid date format: {str(e)}'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # For clients, only show their bookings
        if request.user.role == 'client':
            bookings = Booking.objects.filter(
                client=request.user,
                start_time__gte=start_datetime,
                end_time__lte=end_datetime
            )
        else:
            # Admins and staff see all bookings
            bookings = Booking.objects.filter(
                start_time__gte=start_datetime,
                end_time__lte=end_datetime
            )
        
        # Filter by booking_type if provided
        if booking_type:
            if booking_type not in ['simulator', 'coaching']:
                return Response(
                    {'error': 'booking_type must be either "simulator" or "coaching"'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            bookings = bookings.filter(booking_type=booking_type)
        
        serializer = self.get_serializer(bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get booking statistics (admin only)"""
        if request.user.role not in ['admin', 'staff']:
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        today = timezone.now().date()
        week_ago = today - timedelta(days=7)
        
        stats = {
            'total_bookings': Booking.objects.count(),
            'today_bookings': Booking.objects.filter(start_time__date=today).count(),
            'week_bookings': Booking.objects.filter(start_time__date__gte=week_ago).count(),
            'simulator_bookings': Booking.objects.filter(booking_type='simulator').count(),
            'coaching_bookings': Booking.objects.filter(booking_type='coaching').count(),
            'revenue_today': Booking.objects.filter(
                start_time__date=today
            ).aggregate(total=Sum('total_price'))['total'] or 0,
            'revenue_week': Booking.objects.filter(
                start_time__date__gte=week_ago
            ).aggregate(total=Sum('total_price'))['total'] or 0,
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['get'])
    def check_simulator_availability(self, request):
        """Check available time slots for simulator booking"""
        from simulators.models import SimulatorAvailability
        
        date_str = request.query_params.get('date')
        duration_minutes = request.query_params.get('duration', 60)
        
        if not date_str:
            return Response(
                {'error': 'Date is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            duration_minutes = int(duration_minutes)
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            # Get day of week (0=Monday, 6=Sunday)
            day_of_week = booking_date.weekday()
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid date or duration format'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get all active simulators (bays 1-5, excluding coaching bay)
        available_simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        ).order_by('bay_number')
        
        if not available_simulators.exists():
            return Response({
                'available_slots': [],
                'message': 'No simulators available'
            })
        
        # Get simulator availability for this day of week
        simulator_availabilities = SimulatorAvailability.objects.filter(
            simulator__in=available_simulators,
            day_of_week=day_of_week
        ).select_related('simulator').order_by('simulator', 'start_time')
        
        if not simulator_availabilities.exists():
            return Response({
                'available_slots': [],
                'message': 'No simulators available for this day'
            })
        
        slot_interval = 30  # minutes
        available_slots = []
        
        # Group availability by simulator
        availability_by_simulator = {}
        for avail in simulator_availabilities:
            if avail.simulator not in availability_by_simulator:
                availability_by_simulator[avail.simulator] = []
            availability_by_simulator[avail.simulator].append(avail)
        
        # Generate slots from availability windows
        for simulator in available_simulators:
            if simulator not in availability_by_simulator:
                continue
            
            for sim_avail in availability_by_simulator[simulator]:
                # Generate slots within this availability window
                avail_start = datetime.combine(booking_date, sim_avail.start_time)
                avail_end = datetime.combine(booking_date, sim_avail.end_time)
                
                # Handle case where end_time is before start_time (crosses midnight)
                if avail_end <= avail_start:
                    if sim_avail.end_time < sim_avail.start_time:
                        avail_end = avail_end + timedelta(days=1)
                    else:
                        continue
                
                # Convert to timezone-aware datetime for availability_end_time
                availability_end_datetime = timezone.make_aware(avail_end)
                
                # Generate slots at 30-minute intervals (regardless of requested duration)
                # This allows frontend to validate if selected duration fits
                current_time = avail_start
                while current_time < avail_end:
                    slot_start = timezone.make_aware(current_time)
                    slot_end = slot_start + timedelta(minutes=duration_minutes)
                    slot_fits_duration = slot_end <= availability_end_datetime
                    
                    # Check for conflicting bookings (use requested duration for conflict check)
                    conflicting_bookings = Booking.objects.filter(
                        simulator=simulator,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    
                    if not conflicting_bookings.exists():
                        slot_start_str = slot_start.isoformat()
                        existing_slot = next((s for s in available_slots if s['start_time'] == slot_start_str), None)
                        
                        if not existing_slot:
                            available_slots.append({
                                'start_time': slot_start_str,
                                'end_time': slot_end.isoformat(),
                                'duration_minutes': duration_minutes,
                                'availability_end_time': availability_end_datetime.isoformat(),
                                'fits_duration': slot_fits_duration,
                                'available_simulators': [{
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }],
                                'assigned_simulator': {
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }
                            })
                        else:
                            # Keep the furthest availability end time and mark as fitting if any simulator fits
                            if availability_end_datetime.isoformat() > existing_slot.get('availability_end_time', ''):
                                existing_slot['availability_end_time'] = availability_end_datetime.isoformat()
                            if slot_fits_duration:
                                existing_slot['fits_duration'] = True
                            existing_slot['end_time'] = slot_end.isoformat()
                            # Add simulator to existing slot
                            if simulator.id not in [s['id'] for s in existing_slot['available_simulators']]:
                                existing_slot['available_simulators'].append({
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                })
                    
                    current_time += timedelta(minutes=slot_interval)
        
        # Sort slots by start_time
        available_slots.sort(key=lambda x: x['start_time'])
        
        return Response({
            'date': date_str,
            'duration_minutes': duration_minutes,
            'available_slots': available_slots
        })
    
    @action(detail=False, methods=['get'])
    def check_coaching_availability(self, request):
        """Check available time slots for coaching booking"""
        date_str = request.query_params.get('date')
        package_id = request.query_params.get('package_id')
        coach_id = request.query_params.get('coach_id')
        
        if not date_str:
            return Response(
                {'error': 'Date is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from coaching.models import CoachingPackage
        from users.models import StaffAvailability
        
        # Get coaching bay (bay 6)
        coaching_bay = Simulator.objects.filter(is_coaching_bay=True, is_active=True).first()
        if not coaching_bay:
            return Response({
                'available_slots': [],
                'message': 'Coaching bay not available'
            })
        
        # Build the coach queryset based on package/coach selections
        coaches_qs = User.objects.filter(role__in=['staff', 'admin'], is_active=True)
        selected_package = None
        
        if package_id:
            try:
                selected_package = CoachingPackage.objects.get(id=package_id, is_active=True)
                coaches_qs = selected_package.staff_members.filter(role__in=['staff', 'admin'], is_active=True)
            except CoachingPackage.DoesNotExist:
                return Response(
                    {'error': 'Package not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        if coach_id:
            coaches_qs = coaches_qs.filter(id=coach_id)
            # If a package is selected, ensure the coach belongs to it
            if selected_package and not selected_package.staff_members.filter(id=coach_id).exists():
                return Response(
                    {'error': 'Selected coach is not assigned to this package'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        coaches = list(coaches_qs.distinct())
        
        if not coaches:
            return Response({
                'available_slots': [],
                'message': 'No coaches available'
            })
        
        # Get duration from request (default 60 minutes)
        duration_minutes = int(request.query_params.get('duration', 60))
        
        # Get day of week (0=Monday, 6=Sunday)
        day_of_week = booking_date.weekday()
        
        # Generate time slots based on staff availability for this day of week
        slot_interval = 30  # 30-minute intervals for slot generation
        available_slots_map = {}
        
        # Get staff availability entries for the requested day, scoped to selected coaches
        staff_availabilities = StaffAvailability.objects.filter(
            day_of_week=day_of_week,
            staff__in=coaches
        ).select_related('staff')
        
        availability_by_staff = {}
        for availability in staff_availabilities:
            availability_by_staff.setdefault(availability.staff_id, []).append(availability)
        
        for coach in coaches:
            coach_availabilities = availability_by_staff.get(coach.id, [])
            if not coach_availabilities:
                continue
            
            coach_name = f"{coach.first_name} {coach.last_name}".strip() or coach.username
            
            for staff_avail in coach_availabilities:
                avail_start = datetime.combine(booking_date, staff_avail.start_time)
                avail_end = datetime.combine(booking_date, staff_avail.end_time)
                
                if avail_end <= avail_start:
                    if staff_avail.end_time < staff_avail.start_time:
                        avail_end = avail_end + timedelta(days=1)
                    else:
                        continue
                
                current_time = avail_start
                while current_time + timedelta(minutes=slot_interval) <= avail_end:
                    slot_start = timezone.make_aware(current_time)
                    slot_end = slot_start + timedelta(minutes=duration_minutes)
                    availability_end_datetime = timezone.make_aware(avail_end)
                    slot_fits_duration = slot_end <= availability_end_datetime
                    
                    # Check conflicts for coach
                    conflicting_bookings = Booking.objects.filter(
                        coach=coach,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    if conflicting_bookings.exists():
                        current_time += timedelta(minutes=slot_interval)
                        continue
                    
                    # Check conflicts for coaching bay
                    bay_conflicts = Booking.objects.filter(
                        simulator=coaching_bay,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    
                    assigned_bay_number = coaching_bay.bay_number
                    if bay_conflicts.exists():
                        # Try any other available non-coaching bay
                        other_bay = Simulator.objects.filter(
                            is_active=True,
                            is_coaching_bay=False
                        ).exclude(
                            id__in=Booking.objects.filter(
                                start_time__lt=slot_end,
                                end_time__gt=slot_start,
                                status__in=['confirmed', 'completed']
                            ).values_list('simulator_id', flat=True)
                        ).first()
                        
                        if not other_bay:
                            current_time += timedelta(minutes=slot_interval)
                            continue
                        
                        assigned_bay_number = other_bay.bay_number
                    
                    slot_key = slot_start.isoformat()
                    slot_entry = available_slots_map.get(slot_key)
                    
                    if not slot_entry:
                        slot_entry = {
                            'start_time': slot_key,
                            'end_time': slot_end.isoformat(),
                            'duration_minutes': duration_minutes,
                            'availability_end_time': availability_end_datetime.isoformat(),
                            'fits_duration': slot_fits_duration,
                            'available_coaches': []
                        }
                        available_slots_map[slot_key] = slot_entry
                    else:
                        # Keep the furthest availability end time and mark as fitting if any coach fits
                        if availability_end_datetime.isoformat() > slot_entry['availability_end_time']:
                            slot_entry['availability_end_time'] = availability_end_datetime.isoformat()
                        if slot_fits_duration:
                            slot_entry['fits_duration'] = True
                        slot_entry['end_time'] = slot_end.isoformat()
                    
                    if coach.id not in [c['id'] for c in slot_entry['available_coaches']]:
                        slot_entry['available_coaches'].append({
                            'id': coach.id,
                            'name': coach_name,
                            'email': coach.email,
                            'assigned_bay': assigned_bay_number
                        })
                    
                    current_time += timedelta(minutes=slot_interval)
        
        available_slots = sorted(available_slots_map.values(), key=lambda x: x['start_time'])
        
        return Response({
            'date': date_str,
            'package_id': package_id,
            'coach_id': coach_id,
            'available_slots': available_slots
        })