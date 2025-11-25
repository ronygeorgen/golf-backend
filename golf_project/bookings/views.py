from decimal import Decimal, ROUND_HALF_UP
from rest_framework import viewsets, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers
from django.db import transaction
from django.db.models import Q, Sum, F
from django.utils import timezone
from datetime import datetime, timedelta
from .models import Booking
from .serializers import BookingSerializer, BookingCreateSerializer
from users.models import User
from simulators.models import Simulator, SimulatorCredit
from coaching.models import CoachingPackagePurchase

class TenPerPagePagination(PageNumberPagination):
    page_size = 10
    page_query_param = 'page'
    page_size_query_param = None


class FivePerPagePagination(PageNumberPagination):
    page_size = 5
    page_query_param = 'page'
    page_size_query_param = None


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    lock_window = timedelta(hours=24)
    pagination_class = TenPerPagePagination
    
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
    
    def _find_optimal_simulator(self, start_time, end_time):
        """Assign the best simulator by filling gaps and balancing usage."""
        active_simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        ).order_by('bay_number')
        
        best_choice = None
        for simulator in active_simulators:
            conflict_exists = Booking.objects.filter(
                simulator=simulator,
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed'],
                booking_type='simulator'
            ).exists()
            
            if conflict_exists:
                continue
            
            previous_booking = Booking.objects.filter(
                simulator=simulator,
                end_time__lte=start_time,
                booking_type='simulator',
                status__in=['confirmed', 'completed']
            ).order_by('-end_time').first()
            
            next_booking = Booking.objects.filter(
                simulator=simulator,
                start_time__gte=end_time,
                booking_type='simulator',
                status__in=['confirmed', 'completed']
            ).order_by('start_time').first()
            
            gap_before = (start_time - previous_booking.end_time).total_seconds() / 60 if previous_booking else 24 * 60
            gap_after = (next_booking.start_time - end_time).total_seconds() / 60 if next_booking else 24 * 60
            day_usage = Booking.objects.filter(
                simulator=simulator,
                start_time__date=start_time.date(),
                status__in=['confirmed', 'completed']
            ).count()
            
            # Lower scores are preferred. Encourage filling tight gaps first, then balancing day usage.
            score = gap_before + gap_after + (day_usage * 15)
            
            choice = (score, day_usage, simulator.bay_number, simulator)
            if not best_choice or choice < best_choice:
                best_choice = choice
        
        return best_choice[-1] if best_choice else None
    
    def _consume_package_session(self, package):
        # Find an active package purchase for the user
        # Include normal purchases and accepted gifts
        purchase = CoachingPackagePurchase.objects.select_for_update().filter(
            client=self.request.user,
            package=package,
            sessions_remaining__gt=0,
            package_status='active'
        ).exclude(
            # Exclude pending gifts
            gift_status='pending'
        ).order_by('purchased_at').first()
        
        if not purchase:
            raise serializers.ValidationError(
                "You do not have any remaining sessions for the selected package."
            )
        
        # Use the consume_session method which handles status updates
        purchase.consume_session(1)
        return purchase
    
    def perform_create(self, serializer):
        booking_data = serializer.validated_data
        booking_type = booking_data.get('booking_type')
        use_simulator_credit = booking_data.pop('use_simulator_credit', False)
        redeemed_credit = None
        
        with transaction.atomic():
            if booking_type == 'simulator':
                start_time = booking_data.get('start_time')
                end_time = booking_data.get('end_time')
                
                if start_time and end_time:
                    assigned_simulator = booking_data.get('simulator') or self._find_optimal_simulator(start_time, end_time)
                    if not assigned_simulator:
                        raise serializers.ValidationError("No simulators available for this time slot")
                    
                    if use_simulator_credit:
                        redeemed_credit = self._reserve_simulator_credit()
                    
                    booking = serializer.save(
                        client=self.request.user,
                        simulator=assigned_simulator,
                        total_price=Decimal('0.00')
                    )
                    
                    if redeemed_credit:
                        redeemed_credit.status = SimulatorCredit.Status.REDEEMED
                        redeemed_credit.redeemed_at = timezone.now()
                        redeemed_credit.save(update_fields=['status', 'redeemed_at'])
                        booking.simulator_credit_redemption = redeemed_credit
                        booking.total_price = 0
                        booking.save(update_fields=['simulator_credit_redemption', 'total_price', 'updated_at'])
                    else:
                        booking.total_price = self._calculate_simulator_price(
                            assigned_simulator,
                            booking.duration_minutes
                        )
                        booking.save(update_fields=['total_price', 'updated_at'])
                    return
            elif booking_type == 'coaching':
                package = booking_data.get('coaching_package')
                if not package:
                    raise serializers.ValidationError("A coaching package is required for coaching bookings.")
                
                purchase = self._consume_package_session(package)
                serializer.save(
                    client=self.request.user,
                    package_purchase=purchase,
                    total_price=booking_data.get('total_price', 0)
                )
                return
            
            serializer.save(client=self.request.user)
    
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get all upcoming bookings for the current user"""
        booking_type = request.query_params.get('booking_type')
        upcoming_bookings = Booking.objects.filter(
            client=request.user,
            start_time__gte=timezone.now()
        ).exclude(status='cancelled').order_by('start_time')
        if booking_type in ['simulator', 'coaching']:
            upcoming_bookings = upcoming_bookings.filter(booking_type=booking_type)
        
        # Use 5 per page pagination for upcoming bookings
        paginator = FivePerPagePagination()
        page = paginator.paginate_queryset(upcoming_bookings, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
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
    
    def _is_admin(self, user):
        return getattr(user, 'role', None) == 'admin' or getattr(user, 'is_superuser', False)
    
    def _lock_applies(self, booking):
        return booking.start_time - timezone.now() < self.lock_window
    
    def _user_can_manage_booking(self, user, booking):
        if user.role in ['admin', 'staff']:
            return True
        return booking.client_id == user.id
    
    def _reserve_simulator_credit(self):
        credit = SimulatorCredit.objects.select_for_update().filter(
            status=SimulatorCredit.Status.AVAILABLE,
            client=self.request.user
        ).order_by('issued_at').first()
        if not credit:
            raise serializers.ValidationError("No simulator credits available to redeem.")
        return credit

    def _calculate_simulator_price(self, simulator, duration_minutes):
        if not simulator or not duration_minutes:
            return Decimal('0.00')
        if simulator.is_coaching_bay:
            return Decimal('0.00')
        if simulator.hourly_price:
            hours = Decimal(duration_minutes) / Decimal(60)
            price = (Decimal(simulator.hourly_price) * hours).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return price
        from simulators.models import DurationPrice
        try:
            duration_price = DurationPrice.objects.get(duration_minutes=duration_minutes)
            return Decimal(duration_price.price)
        except DurationPrice.DoesNotExist:
            return Decimal('0.00')
    
    def _restore_coaching_session(self, booking):
        purchase = booking.package_purchase
        if not purchase:
            return None
        purchase.sessions_remaining = F('sessions_remaining') + 1
        purchase.save(update_fields=['sessions_remaining', 'updated_at'])
        purchase.refresh_from_db(fields=['sessions_remaining'])
        return purchase.sessions_remaining
    
    def _issue_simulator_credit(self, booking, issued_by=None, reason=SimulatorCredit.Reason.CANCELLATION):
        credit = SimulatorCredit.objects.create(
            client=booking.client,
            reason=reason,
            issued_by=issued_by if issued_by and self._is_admin(issued_by) else None,
            source_booking=booking,
            notes=f"Credit issued for booking #{booking.id}"
        )
        return credit
    
    def update(self, request, *args, **kwargs):
        booking = self.get_object()
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        if self._lock_applies(booking) and not self._is_admin(request.user):
            return Response(
                {'error': 'Bookings within 24 hours cannot be modified. Contact an admin for assistance.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().update(request, *args, **kwargs)
    
    def partial_update(self, request, *args, **kwargs):
        booking = self.get_object()
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        if self._lock_applies(booking) and not self._is_admin(request.user):
            return Response(
                {'error': 'Bookings within 24 hours cannot be modified. Contact an admin for assistance.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().partial_update(request, *args, **kwargs)
    
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
        force_override_value = request.data.get('force_override', False)
        force_override = str(force_override_value).lower() in ['1', 'true', 'yes']
        
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        if booking.status == 'cancelled':
            serializer = self.get_serializer(booking)
            return Response({
                'message': 'Booking already cancelled',
                'booking': serializer.data
            }, status=status.HTTP_200_OK)
        
        lock_applies = self._lock_applies(booking)
        if lock_applies and not (force_override and self._is_admin(request.user)):
            return Response(
                {
                    'error': 'This booking starts within 24 hours and cannot be cancelled online. Contact an admin for assistance.',
                    'lock_applies': True
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            booking.status = 'cancelled'
            booking.save(update_fields=['status', 'updated_at'])
            restitution = {}
            
            if booking.booking_type == 'coaching':
                remaining = self._restore_coaching_session(booking)
                restitution['sessions_remaining'] = remaining
            elif booking.booking_type == 'simulator':
                credit = self._issue_simulator_credit(
                    booking,
                    issued_by=request.user if force_override and self._is_admin(request.user) else None
                )
                restitution['simulator_credit_id'] = credit.id
        
        serializer = self.get_serializer(booking)
        return Response({
            'message': 'Booking cancelled successfully',
            'booking': serializer.data,
            'lock_applies': lock_applies,
            'lock_overridden': lock_applies and force_override and self._is_admin(request.user),
            'restitution': restitution
        })

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Reschedule a booking to a new time"""
        booking = self.get_object()
        force_override_value = request.data.get('force_override', False)
        force_override = str(force_override_value).lower() in ['1', 'true', 'yes']
        
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        lock_applies = self._lock_applies(booking)
        if lock_applies and not (force_override and self._is_admin(request.user)):
            return Response(
                {
                    'error': 'Bookings within 24 hours cannot be rescheduled online. Contact an admin for assistance.',
                    'lock_applies': True
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        incoming_data = request.data.copy()
        incoming_data['booking_type'] = booking.booking_type
        incoming_data.setdefault('duration_minutes', booking.duration_minutes)
        if booking.booking_type == 'coaching':
            if not booking.coaching_package:
                return Response({'error': 'This coaching booking is missing its package reference.'}, status=status.HTTP_400_BAD_REQUEST)
            incoming_data.setdefault('coaching_package', booking.coaching_package_id)
            if booking.coach_id:
                incoming_data.setdefault('coach', booking.coach_id)
        elif booking.booking_type == 'simulator' and booking.simulator_id:
            incoming_data.setdefault('simulator', booking.simulator_id)
        
        serializer = BookingCreateSerializer(
            data=incoming_data,
            context={'exclude_booking_id': booking.id}
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        
        with transaction.atomic():
            booking.start_time = validated['start_time']
            booking.end_time = validated['end_time']
            booking.duration_minutes = validated.get('duration_minutes', booking.duration_minutes)
            
            update_fields = ['start_time', 'end_time', 'duration_minutes', 'updated_at']
            if booking.booking_type == 'simulator':
                assigned_simulator = validated.get('simulator')
                if not assigned_simulator:
                    assigned_simulator = self._find_optimal_simulator(booking.start_time, booking.end_time)
                if not assigned_simulator:
                    raise serializers.ValidationError("No simulators available for this time slot")
                booking.simulator = assigned_simulator
                update_fields.append('simulator')
            elif booking.booking_type == 'coaching':
                if validated.get('coach'):
                    booking.coach = validated['coach']
                    update_fields.append('coach')

            if booking.booking_type == 'simulator' and not booking.simulator_credit_redemption_id:
                booking.total_price = self._calculate_simulator_price(
                    booking.simulator,
                    booking.duration_minutes
                )
                update_fields.append('total_price')
            
            booking.save(update_fields=update_fields)
        
        serializer = self.get_serializer(booking)
        return Response({
            'message': 'Booking rescheduled successfully',
            'booking': serializer.data,
            'lock_applies': lock_applies,
            'lock_overridden': lock_applies and force_override and self._is_admin(request.user)
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
        show_bay_details = request.user.role in ['admin', 'staff']
        
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
                            slot_payload = {
                                'slot_id': f"{slot_start_str}:{duration_minutes}",
                                'start_time': slot_start_str,
                                'end_time': slot_end.isoformat(),
                                'duration_minutes': duration_minutes,
                                'availability_end_time': availability_end_datetime.isoformat(),
                                'fits_duration': slot_fits_duration,
                                'bay_count': 1,
                            }
                            if show_bay_details:
                                slot_payload['available_simulators'] = [{
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }]
                                slot_payload['assigned_simulator'] = {
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }
                            available_slots.append(slot_payload)
                        else:
                            # Keep the furthest availability end time and mark as fitting if any simulator fits
                            if availability_end_datetime.isoformat() > existing_slot.get('availability_end_time', ''):
                                existing_slot['availability_end_time'] = availability_end_datetime.isoformat()
                            if slot_fits_duration:
                                existing_slot['fits_duration'] = True
                            existing_slot['end_time'] = slot_end.isoformat()
                            existing_slot['bay_count'] = existing_slot.get('bay_count', 1) + 1
                            if show_bay_details:
                                if simulator.id not in [s['id'] for s in existing_slot.get('available_simulators', [])]:
                                    existing_slot.setdefault('available_simulators', []).append({
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
        
        if not package_id:
            return Response(
                {'error': 'package_id is required'},
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
        
        # Session duration is dictated by package; allow optional override only if it matches
        requested_duration = request.query_params.get('duration')
        if requested_duration:
            duration_minutes = int(requested_duration)
            if duration_minutes != selected_package.session_duration_minutes:
                return Response(
                    {'error': f'Coaching sessions for this package must be {selected_package.session_duration_minutes} minutes.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            duration_minutes = selected_package.session_duration_minutes
        
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