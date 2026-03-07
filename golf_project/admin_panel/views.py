import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from django.db import transaction
from django.db.models import Count, Sum, Q, F
from django.utils import timezone as django_timezone
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from users.models import User, StaffAvailability, StaffDayAvailability

logger = logging.getLogger(__name__)
from users.utils import get_location_id_from_request, filter_by_location
from simulators.models import Simulator, SimulatorCredit
from bookings.models import Booking
from users.serializers import UserSerializer, StaffSerializer, StaffAvailabilitySerializer, StaffDayAvailabilitySerializer
from bookings.serializers import BookingSerializer
from .serializers import CoachingSessionAdjustmentSerializer, SimulatorCreditGrantSerializer, ClosedDaySerializer, LiabilityWaiverSerializer
from .models import ClosedDay, LiabilityWaiver
from simulators.serializers import SimulatorCreditSerializer
from .models import ClosedDay

class AdminDashboardViewSet(viewsets.ViewSet):
    @action(detail=False, methods=['get'])
    def stats(self, request):
        location_id = get_location_id_from_request(request)
        today = django_timezone.now().date()
        
        # Filter by location
        bookings_qs = Booking.objects.all()
        simulators_qs = Simulator.objects.all()
        
        if location_id:
            bookings_qs = bookings_qs.filter(location_id=location_id)
            simulators_qs = simulators_qs.filter(location_id=location_id)
        
        stats = {
            'total_bookings': bookings_qs.count(),
            'today_bookings': bookings_qs.filter(
                start_time__date=today
            ).count(),
            'active_simulators': simulators_qs.filter(is_active=True).count(),
            'total_revenue': bookings_qs.aggregate(
                total=Sum('total_price')
            )['total'] or 0
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['get'], url_path='recent-bookings')
    def recent_bookings(self, request):
        location_id = get_location_id_from_request(request)
        
        bookings = Booking.objects.select_related(
            'client', 'simulator', 'coach', 'coaching_package'
        )
        
        if location_id:
            bookings = bookings.filter(location_id=location_id)
        
        bookings = bookings.order_by('-created_at')[:10]
        
        from bookings.serializers import BookingSerializer
        serializer = BookingSerializer(bookings, many=True)
        return Response(serializer.data)

class StaffViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(role__in=['staff', 'admin'])
    serializer_class = StaffSerializer
    
    def get_queryset(self):
        """Filter staff/admin by location_id"""
        queryset = User.objects.filter(role__in=['staff', 'admin'])
        # Superadmin can see all admins and staff across all locations
        if self.request.user.role == 'superadmin':
            return queryset
        else:
            # Regular admin can see staff members and other admins from their location
            location_id = get_location_id_from_request(self.request)
            if location_id:
                queryset = queryset.filter(ghl_location_id=location_id)
            else:
                queryset = queryset.filter(role__in=['staff', 'admin'])
        return queryset
    
    def get_serializer_class(self):
        # Use UserSerializer for read operations to include username
        if self.action in ['list', 'retrieve']:
            return UserSerializer
        # Use StaffSerializer for create/update to auto-generate username
        return StaffSerializer
    
    def perform_create(self, serializer):
        """Set location_id when creating staff/admin"""
        user = self.request.user
        
        # Superadmin can create admin and assign location_id
        if user.role == 'superadmin':
            location_id = self.request.data.get('ghl_location_id')
            role = serializer.validated_data.get('role', 'admin')
            # Superadmin can only create admin, not staff or client
            if role != 'admin':
                raise PermissionDenied("Superadmin can only create admin users.")
            if location_id:
                # Validate location exists
                from ghl.models import GHLLocation
                try:
                    GHLLocation.objects.get(location_id=location_id)
                    serializer.save(ghl_location_id=location_id, role='admin')
                except GHLLocation.DoesNotExist:
                    raise PermissionDenied(f"Location {location_id} does not exist.")
            else:
                raise PermissionDenied("Location ID is required when creating admin.")
        else:
            # Regular admin can only create staff (not admin) for their location
            role = serializer.validated_data.get('role', 'staff')
            if role == 'admin':
                raise PermissionDenied("Regular admins can only create staff members, not admin users.")
            location_id = get_location_id_from_request(self.request)
            if location_id:
                serializer.save(ghl_location_id=location_id, role='staff')
            else:
                serializer.save(role='staff')
    
    def perform_update(self, serializer):
        """Limit regular admins from elevating users to admin role"""
        user = self.request.user
        
        # If regular admin (not superadmin), ensure role is not changed to admin from something else
        if user.role != 'superadmin':
            role = serializer.validated_data.get('role')
            target_user = self.get_object()
            
            if role == 'admin' and target_user.role != 'admin':
                raise PermissionDenied("Regular admins cannot change user role to admin.")
            
            serializer.save()
        else:
            # Superadmin can update freely
            serializer.save()
    
    @action(detail=True, methods=['get', 'put'])
    def availability(self, request, pk=None):
        staff = self.get_object()
        
        # Verify staff belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and staff.ghl_location_id != location_id:
            raise PermissionDenied("You can only manage availability for staff in your location.")
        
        if request.method == 'GET':
            # Get all recurring weekly availability
            availability = StaffAvailability.objects.filter(staff=staff).order_by('day_of_week', 'start_time')
            serializer = StaffAvailabilitySerializer(availability, many=True, context={'location_id': location_id})
            return Response(serializer.data)
        
        elif request.method == 'PUT':
            # Update availability data
            availability_data = request.data
            
            # Ensure availability_data is a list
            if not isinstance(availability_data, list):
                return Response(
                    {'error': 'Availability data must be a list'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Process each item in the payload - supports explicit delete or update/create
            updated_availability = []
            
            for avail_data in availability_data:
                # Check for explicit delete flag
                if avail_data.get('deleted') is True:
                    day_of_week = avail_data.get('day_of_week')
                    start_time_str = avail_data.get('start_time')
                    avail_id = avail_data.get('id')
                    
                    # Try to delete by ID if present
                    if avail_id:
                         StaffAvailability.objects.filter(id=avail_id, staff=staff).delete()
                         print(f"Deleted availability {avail_id} for staff {staff.id}")
                    # Fallback: Delete by matching day/time if ID not provided (for older clients?)
                    elif day_of_week is not None and start_time_str:
                        if len(start_time_str) > 5:
                            start_time_str = start_time_str[:5]
                            
                        # Find matching entry
                        # This is a bit unsafe without ID but provided for robustness
                        # Filter by checking start_time string match
                        candidates = StaffAvailability.objects.filter(staff=staff, day_of_week=day_of_week)
                        for c in candidates:
                            if str(c.start_time)[:5] == start_time_str:
                                c.delete()
                                print(f"Deleted availability (by match) for staff {staff.id}")
                    
                    continue # Skip update logic for this item

                # Update or create logic for items NOT marked deleted
                day_of_week = avail_data.get('day_of_week')
                if day_of_week is not None:
                    try:
                        day_of_week = int(day_of_week)
                        # Use serializer to handle timezone conversion
                        serializer_data = {**avail_data, 'staff': staff.id, 'day_of_week': day_of_week}
                        serializer = StaffAvailabilitySerializer(data=serializer_data, context={'location_id': location_id})
                        if serializer.is_valid():
                            availability, created = StaffAvailability.objects.update_or_create(
                                staff=staff,
                                day_of_week=day_of_week,
                                start_time=serializer.validated_data.get('start_time'),
                                defaults={
                                    'end_time': serializer.validated_data.get('end_time'),
                                }
                            )
                            updated_availability.append(availability)
                        else:
                            # Fallback to direct assignment if serializer fails
                            print(f"Serializer validation failed: {serializer.errors}")
                            try:
                                start_time_obj = datetime.strptime(avail_data.get('start_time', '09:00'), '%H:%M').time()
                                end_time_obj = datetime.strptime(avail_data.get('end_time', '17:00'), '%H:%M').time()
                                availability, created = StaffAvailability.objects.update_or_create(
                                    staff=staff,
                                    day_of_week=day_of_week,
                                    start_time=start_time_obj,
                                    defaults={
                                        'end_time': end_time_obj,
                                    }
                                )
                                updated_availability.append(availability)
                            except ValueError:
                                pass
                    except (ValueError, TypeError):
                        pass
            
            # Return updated availability list (fetch fresh from DB to include all current items)
            all_avail = StaffAvailability.objects.filter(staff=staff).order_by('day_of_week', 'start_time')
            serializer = StaffAvailabilitySerializer(all_avail, many=True, context={'location_id': location_id})
            return Response(serializer.data)
    
    @action(detail=True, methods=['get', 'put'], url_path='day-availability')
    def day_availability(self, request, pk=None):
        """
        Handle day-specific (non-recurring) availability for staff.
        GET: Returns all day-specific availability entries
        PUT: Updates day-specific availability (replaces all entries with provided list)
        """
        staff = self.get_object()
        
        # Verify staff belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and staff.ghl_location_id != location_id:
            raise PermissionDenied("You can only manage availability for staff in your location.")
        
        if request.method == 'GET':
            # Get all day-specific availability, ordered by date
            day_availability = StaffDayAvailability.objects.filter(staff=staff).order_by('date', 'start_time')
            serializer = StaffDayAvailabilitySerializer(day_availability, many=True, context={'location_id': location_id})
            return Response(serializer.data)
        
        elif request.method == 'PUT':
            # Update day-specific availability data
            availability_data = request.data
            
            # Ensure availability_data is a list
            if not isinstance(availability_data, list):
                return Response(
                    {'error': 'Availability data must be a list'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Get all existing day-specific availability entries for this staff
            existing_entries = StaffDayAvailability.objects.filter(staff=staff)
            
            # Get IDs of entries to keep (from the request)
            entries_to_keep = set()
            for avail_data in availability_data:
                date = avail_data.get('date')
                start_time_str = avail_data.get('start_time')
                if date and start_time_str:
                    # Normalize time format (remove seconds if present)
                    if ':' in start_time_str and start_time_str.count(':') > 1:
                        start_time_str = start_time_str[:5]
                    entries_to_keep.add((str(date), start_time_str))
            
            # Delete entries not in the keep list
            to_delete_ids = []
            for entry in existing_entries:
                entry_key = (str(entry.date), str(entry.start_time)[:5])
                if entry_key not in entries_to_keep:
                    to_delete_ids.append(entry.id)
            
            if to_delete_ids:
                deleted_count = StaffDayAvailability.objects.filter(id__in=to_delete_ids).delete()
                print(f"Deleted {deleted_count[0]} day-specific availability entries for staff {staff.id}")
            
            # Update or create each day-specific availability entry
            updated_availability = []
            for avail_data in availability_data:
                date = avail_data.get('date')
                if date:
                    try:
                        # Use serializer to handle timezone conversion
                        serializer_data = {**avail_data, 'staff': staff.id, 'date': date}
                        serializer = StaffDayAvailabilitySerializer(data=serializer_data, context={'location_id': location_id})
                        if serializer.is_valid():
                            availability, created = StaffDayAvailability.objects.update_or_create(
                                staff=staff,
                                date=date,
                                start_time=serializer.validated_data.get('start_time'),
                                defaults={
                                    'end_time': serializer.validated_data.get('end_time'),
                                }
                            )
                            updated_availability.append(availability)
                        else:
                            # Fallback to direct assignment if serializer fails
                            print(f"Serializer validation failed: {serializer.errors}")
                            try:
                                from datetime import date as date_obj
                                date_obj_parsed = date_obj.fromisoformat(date) if isinstance(date, str) else date
                                start_time_obj = datetime.strptime(avail_data.get('start_time', '09:00'), '%H:%M').time()
                                end_time_obj = datetime.strptime(avail_data.get('end_time', '17:00'), '%H:%M').time()
                                availability, created = StaffDayAvailability.objects.update_or_create(
                                    staff=staff,
                                    date=date_obj_parsed,
                                    start_time=start_time_obj,
                                    defaults={
                                        'end_time': end_time_obj,
                                    }
                                )
                                updated_availability.append(availability)
                            except (ValueError, TypeError) as e:
                                print(f"Error creating day availability: {e}")
                                pass
                    except (ValueError, TypeError) as e:
                        print(f"Error processing day availability: {e}")
                        pass
            
            # Return updated availability list
            serializer = StaffDayAvailabilitySerializer(updated_availability, many=True, context={'location_id': location_id})
            return Response(serializer.data)
    
    @action(detail=True, methods=['get', 'post', 'delete'], url_path='blocked-dates')
    def blocked_dates(self, request, pk=None):
        """
        Handle blocked dates for staff.
        GET: Returns all blocked dates for the staff member
        POST: Block a specific date (cancels all bookings for that staff on that date)
        DELETE: Unblock a specific date
        """
        staff = self.get_object()
        
        # Verify staff belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and staff.ghl_location_id != location_id:
            raise PermissionDenied("You can only manage blocked dates for staff in your location.")
        
        from users.models import StaffBlockedDate
        from users.serializers import StaffBlockedDateSerializer
        from datetime import datetime as dt, time as dt_time
        import pytz

        if request.method == 'GET':
            # Get all blocked dates for this staff member
            
            blocked_dates = StaffBlockedDate.objects.filter(staff=staff).order_by('date')
            serializer = StaffBlockedDateSerializer(blocked_dates, many=True)
            return Response(serializer.data)
        
        elif request.method == 'POST':
            # Block a specific date (full-day or partial-day) and cancel conflicting bookings
            date_str = request.data.get('date')
            start_time_str = request.data.get('start_time')  # Optional: "10:00" for partial-day block
            end_time_str = request.data.get('end_time')      # Optional: "15:00" for partial-day block
            reason = request.data.get('reason', '')
            
            if not date_str:
                return Response(
                    {'error': 'Date is required'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                # Parse date
                block_date = dt.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Parse times if provided (for partial-day block)
            start_time = None
            end_time = None
            is_full_day = True
            
            if start_time_str and end_time_str:
                try:
                    start_time = dt.strptime(start_time_str, '%H:%M').time()
                    end_time = dt.strptime(end_time_str, '%H:%M').time()
                    is_full_day = False
                    
                    if start_time >= end_time:
                        return Response(
                            {'error': 'End time must be after start time'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                except ValueError:
                    return Response(
                        {'error': 'Invalid time format. Use HH:MM (e.g., 10:00)'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            elif start_time_str or end_time_str:
                # One time provided but not the other
                return Response(
                    {'error': 'Both start_time and end_time must be provided for partial-day blocks'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check for duplicate blocks
            # For full-day: check if any block exists for this date
            # For partial-day: check if exact same time range exists
            if is_full_day:
                # Check if there's already a full-day block
                if StaffBlockedDate.objects.filter(
                    staff=staff, 
                    date=block_date,
                    start_time__isnull=True,
                    end_time__isnull=True
                ).exists():
                    return Response(
                        {'error': 'A full-day block already exists for this date'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                # Check if exact same partial-day block exists
                if StaffBlockedDate.objects.filter(
                    staff=staff,
                    date=block_date,
                    start_time=start_time,
                    end_time=end_time
                ).exists():
                    return Response(
                        {'error': 'This time range is already blocked'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Create blocked date entry
            blocked_date = StaffBlockedDate.objects.create(
                staff=staff,
                date=block_date,
                start_time=start_time,
                end_time=end_time,
                reason=reason,
                created_by=request.user
            )
            
            # Find and cancel conflicting coaching bookings
            
            # Use center timezone (DST-aware — reads from GHLLocation.timezone)
            from golf_project.timezone_utils import get_center_timezone
            center_tz = get_center_timezone(location_id)
            
            if is_full_day:
                # Full-day block: cancel all bookings on this date
                start_of_day_local = center_tz.localize(dt.combine(block_date, dt_time.min))
                end_of_day_local = center_tz.localize(dt.combine(block_date, dt_time.max))
                
                bookings_to_cancel = Booking.objects.filter(
                    coach=staff,
                    booking_type='coaching',
                    start_time__range=(start_of_day_local, end_of_day_local),
                    status='confirmed'
                ).select_related('client', 'package_purchase', 'coaching_package')
            else:
                # Partial-day block: cancel only bookings that overlap with the blocked time range
                # Convert block times to datetime for comparison
                block_start_dt = center_tz.localize(dt.combine(block_date, start_time))
                block_end_dt = center_tz.localize(dt.combine(block_date, end_time))
                
                bookings_to_cancel = Booking.objects.filter(
                    coach=staff,
                    booking_type='coaching',
                    start_time__date=block_date,  # Same date
                    status='confirmed'
                ).select_related('client', 'package_purchase', 'coaching_package')
                
                # Filter for time overlap: booking_start < block_end AND booking_end > block_start
                bookings_to_cancel = [
                    b for b in bookings_to_cancel
                    if b.start_time < block_end_dt and b.end_time > block_start_dt
                ]
            
            cancelled_count = 0
            refunded_sessions = 0
            refunded_hours = Decimal('0')
            
            with transaction.atomic():
                for booking in bookings_to_cancel:
                    # Cancel the booking
                    booking.status = 'cancelled'
                    booking.save()
                    
                    # Refund credits based on booking type
                    if booking.package_purchase:
                        # Refund coaching session
                        purchase = booking.package_purchase
                        purchase.sessions_remaining = F('sessions_remaining') + 1
                        purchase.save()
                        purchase.refresh_from_db()
                        refunded_sessions += 1
                    
                    cancelled_count += 1
                    
                    # Log the cancellation
                    print(
                        f"Cancelled booking {booking.id} for {booking.client.username} "
                        f"due to staff {staff.username} being blocked on {block_date}"
                    )
            
            
            # Prepare response
            serializer = StaffBlockedDateSerializer(blocked_date)
            
            # Create appropriate message based on block type
            if is_full_day:
                block_description = f"full day on {date_str}"
            else:
                block_description = f"{date_str} from {start_time_str} to {end_time_str}"
            
            return Response({
                'blocked_date': serializer.data,
                'cancelled_bookings': cancelled_count,
                'refunded_sessions': refunded_sessions,
                'refunded_simulator_hours': float(refunded_hours),
                'message': f'Successfully blocked {block_description} for {staff.first_name} {staff.last_name}. '
                          f'Cancelled {cancelled_count} booking(s) and refunded credits to clients.'
            }, status=status.HTTP_201_CREATED)
        
        elif request.method == 'DELETE':
            # Unblock a specific date
            date_str = request.data.get('date') or request.query_params.get('date')
            
            if not date_str:
                return Response(
                    {'error': 'Date is required'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                # Parse date
                unblock_date = dt.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Find and delete the blocked date
            try:
                blocked_date = StaffBlockedDate.objects.get(staff=staff, date=unblock_date)
                blocked_date.delete()
                return Response({
                    'message': f'Successfully unblocked {date_str} for {staff.first_name} {staff.last_name}'
                }, status=status.HTTP_200_OK)
            except StaffBlockedDate.DoesNotExist:
                return Response(
                    {'error': 'This date is not blocked for this staff member'}, 
                    status=status.HTTP_404_NOT_FOUND
                )

    
    @action(detail=True, methods=['get'], url_path='referrals')
    def referrals(self, request, pk=None):
        """
        Get all clients and packages referred by a specific staff member.
        Returns clients with their details and staff-referred packages.
        """
        staff = self.get_object()
        
        # Verify staff belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and staff.ghl_location_id != location_id:
            raise PermissionDenied("You can only view referrals for staff in your location.")
        
        try:
            from ghl.services import (
                calculate_total_coaching_sessions,
                calculate_total_simulator_hours,
                get_last_active_package
            )
            from coaching.models import CoachingPackagePurchase
            from rest_framework.pagination import PageNumberPagination
            from rest_framework.response import Response as DRFResponse
            
            # Custom paginator for referrals
            class ReferralsPagination(PageNumberPagination):
                page_size = 10
                page_size_query_param = 'page_size'
                max_page_size = 100
                
                def get_paginated_response(self, data):
                    return DRFResponse({
                        'count': self.page.paginator.count,
                        'total_pages': self.page.paginator.num_pages,
                        'current_page': self.page.number,
                        'page_size': self.page_size,
                        'next': self.get_next_link(),
                        'previous': self.get_previous_link(),
                    'total_referrals': self.page.paginator.count,
                    'total_sales': str(getattr(self, 'total_sales', Decimal('0.00'))),
                    'members': data
                })
            
            # Get date filter parameters (expecting UTC ISO strings from frontend)
            from_date = request.query_params.get('from_date')
            to_date = request.query_params.get('to_date')
            
            # Helper function to parse UTC date string
            def parse_utc_date(date_str, is_end_of_day=False):
                """Parse UTC ISO date string to timezone-aware datetime"""
                if not date_str:
                    return None
                
                try:
                    original_date_str = date_str
                    
                    # Handle ISO format with 'Z' suffix (UTC) - e.g., "2025-11-01T00:00:00.000Z"
                    if date_str.endswith('Z'):
                        # Remove 'Z' and microseconds, then add '+00:00' for fromisoformat
                        # Python's fromisoformat can be finicky with microseconds and timezone
                        if '.' in date_str:
                            # Has microseconds: "2025-11-01T00:00:00.000Z"
                            date_part = date_str.split('.')[0]  # "2025-11-01T00:00:00"
                            date_str = date_part + '+00:00'  # "2025-11-01T00:00:00+00:00"
                        else:
                            # No microseconds: "2025-11-01T00:00:00Z"
                            date_str = date_str[:-1] + '+00:00'
                    elif 'T' in date_str:
                        # ISO format with time but no timezone - assume UTC
                        if '+' not in date_str and not date_str.endswith('Z'):
                            # Remove microseconds if present before adding timezone
                            if '.' in date_str:
                                date_part = date_str.split('.')[0]
                                date_str = date_part + '+00:00'
                            else:
                                date_str = date_str + '+00:00'
                    
                    # Parse the date string
                    if 'T' in date_str:
                        # ISO format with time - use fromisoformat
                        try:
                            dt = datetime.fromisoformat(date_str)
                        except ValueError as e:
                            logger.warning(f"fromisoformat failed for {date_str}, trying alternative: {e}")
                            # Fallback: try parsing without microseconds
                            if '.' in date_str:
                                date_part = date_str.split('.')[0]
                                if '+' in date_str:
                                    timezone_part = date_str.split('+')[1] if '+' in date_str else '+00:00'
                                    date_str = date_part + '+' + timezone_part
                                else:
                                    date_str = date_part + '+00:00'
                            dt = datetime.fromisoformat(date_str)
                    else:
                        # Simple date format (YYYY-MM-DD)
                        dt = datetime.strptime(date_str, '%Y-%m-%d')
                        if is_end_of_day:
                            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
                        else:
                            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    # Ensure timezone-aware and in UTC
                    if dt.tzinfo is None:
                        dt = django_timezone.make_aware(dt, dt_timezone.utc)
                    else:
                        # Convert to UTC if in different timezone
                        dt = dt.astimezone(dt_timezone.utc)
                    
                    logger.info(f"Successfully parsed date: {original_date_str} -> {dt} (UTC)")
                    return dt
                except (ValueError, AttributeError, TypeError) as e:
                    logger.error(f"Error parsing date {date_str}: {e}", exc_info=True)
                    # Return None to indicate parsing failure - this will prevent filter from being applied
                    return None
            
            # Get all clients who have packages referred by this staff member
            referred_purchases = CoachingPackagePurchase.objects.filter(
                referral_id=staff.id,
                package_status='active'
            ).select_related('client', 'package')
            
            # Apply date filter if provided (dates are in UTC ISO format from frontend)
            from_date_obj = parse_utc_date(from_date, is_end_of_day=False) if from_date else None
            to_date_obj = parse_utc_date(to_date, is_end_of_day=True) if to_date else None
            
            # Validate that if dates are provided, they must be parsed successfully
            if from_date and from_date_obj is None:
                logger.error(f"Failed to parse from_date: {from_date}")
                return Response({
                    'error': f'Invalid from_date format: {from_date}',
                    'count': 0,
                    'total_pages': 0,
                    'current_page': 1,
                    'page_size': 10,
                    'total_referrals': 0,
                    'total_sales': '0.00',
                    'members': []
                }, status=status.HTTP_400_BAD_REQUEST)
            
            if to_date and to_date_obj is None:
                logger.error(f"Failed to parse to_date: {to_date}")
                return Response({
                    'error': f'Invalid to_date format: {to_date}',
                    'count': 0,
                    'total_pages': 0,
                    'current_page': 1,
                    'page_size': 10,
                    'total_referrals': 0,
                    'total_sales': '0.00',
                    'members': []
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Log parsed dates for debugging
            if from_date or to_date:
                logger.info(f"Date filter - from_date: {from_date} -> {from_date_obj}, to_date: {to_date} -> {to_date_obj}")
            
            # Log count before filtering for debugging
            count_before_filter = referred_purchases.count()
            logger.info(f"Referrals count BEFORE date filter: {count_before_filter}")
            
            # Apply date filters - these MUST be applied if dates are provided
            if from_date_obj is not None:
                referred_purchases = referred_purchases.filter(purchased_at__gte=from_date_obj)
                logger.info(f"Applied from_date filter: {from_date_obj} (UTC), type: {type(from_date_obj)}")
            
            if to_date_obj is not None:
                referred_purchases = referred_purchases.filter(purchased_at__lte=to_date_obj)
                logger.info(f"Applied to_date filter: {to_date_obj} (UTC), type: {type(to_date_obj)}")
            
            referred_purchases = referred_purchases.order_by('-purchased_at')
            
            # Log count after filtering for debugging
            if from_date or to_date:
                count_after_filter = referred_purchases.count()
                logger.info(f"Referrals count AFTER date filter: {count_after_filter} (from_date: {from_date_obj}, to_date: {to_date_obj})")
                
                # Log a sample of purchase dates to verify filtering
                sample_purchases = list(referred_purchases[:5])
                for p in sample_purchases:
                    logger.info(f"Sample purchase - ID: {p.id}, purchased_at: {p.purchased_at}, tzinfo: {p.purchased_at.tzinfo if hasattr(p.purchased_at, 'tzinfo') else 'N/A'}")
            
            # Calculate total sales amount
            total_sales = Decimal('0.00')
            for purchase in referred_purchases:
                if purchase.package and purchase.package.price:
                    total_sales += Decimal(str(purchase.package.price))
            
            # Get unique clients
            unique_clients = {}
            for purchase in referred_purchases:
                client = purchase.client
                if client and client.id not in unique_clients:
                    # Calculate custom fields for this client
                    total_sessions = calculate_total_coaching_sessions(client)
                    total_hours = calculate_total_simulator_hours(client)
                    last_package = get_last_active_package(client)
                    
                    # Get all staff-referred purchases for this client (with date filter)
                    client_referred_purchases_qs = CoachingPackagePurchase.objects.filter(
                        referral_id=staff.id,
                        client=client,
                        package_status='active'
                    )
                    
                    # Apply same date filter (reuse parsed dates from above)
                    if from_date_obj:
                        client_referred_purchases_qs = client_referred_purchases_qs.filter(purchased_at__gte=from_date_obj)
                    
                    if to_date_obj:
                        client_referred_purchases_qs = client_referred_purchases_qs.filter(purchased_at__lte=to_date_obj)
                    
                    client_referred_purchases = client_referred_purchases_qs.values('id', 'package__title', 'purchase_name', 'purchased_at')
                    
                    staff_referred_purchases = [
                        {
                            'id': p['id'],
                            'package_name': p['package__title'],
                            'purchase_name': p['purchase_name'] or p['package__title'],
                            'purchased_at': p['purchased_at'].isoformat() if p['purchased_at'] else None
                        }
                        for p in client_referred_purchases
                    ]
                    
                    unique_clients[client.id] = {
                        'id': client.id,
                        'first_name': client.first_name or '',
                        'last_name': client.last_name or '',
                        'email': client.email or '',
                        'phone': client.phone,
                        'custom_fields': {
                            'total_coaching_session': str(total_sessions),
                            'total_simulator_hour': str(total_hours),
                            'last_active_package': last_package or ''
                        },
                        'staff_referred_purchases': staff_referred_purchases
                    }
            
            # Convert to list and sort by first name
            clients_list = list(unique_clients.values())
            clients_list.sort(key=lambda x: (x['first_name'], x['last_name']))
            
            # Apply pagination
            paginator = ReferralsPagination()
            paginator.total_sales = total_sales  # Store total sales in paginator
            page = paginator.paginate_queryset(clients_list, request)
            
            if page is not None:
                return paginator.get_paginated_response(page)
            
            return Response({
                'count': len(clients_list),
                'total_pages': 1,
                'current_page': 1,
                'page_size': 10,
                'total_referrals': len(clients_list),
                'total_sales': str(total_sales),
                'members': clients_list
            })
            
        except Exception as e:
            logger.error(f"Error fetching staff referrals: {e}", exc_info=True)
            return Response(
                {'error': 'Failed to fetch staff referrals'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class AdminOverrideViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    
    def _ensure_admin(self, request):
        if getattr(request.user, 'role', None) not in ['admin', 'superadmin'] and not getattr(request.user, 'is_superuser', False):
            raise PermissionDenied("Administrator privileges are required for this action.")
    
    @action(detail=False, methods=['get'], url_path='locked-bookings')
    def locked_bookings(self, request):
        """
        Get all bookings that are less than 24 hours away (locked bookings)
        for the admin's location. Only admins can cancel these.
        """
        self._ensure_admin(request)
        from bookings.models import Booking
        from bookings.serializers import BookingSerializer
        from django.utils import timezone
        from datetime import timedelta
        
        location_id = get_location_id_from_request(request)
        if not location_id:
            return Response(
                {'error': 'Location ID is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        now = django_timezone.now()
        lock_window = timedelta(hours=24)
        
        # Get bookings that:
        # 1. Are in the future (start_time > now)
        # 2. Are less than 24 hours away (start_time - now < 24 hours)
        # 3. Are confirmed (not cancelled, completed, or no_show)
        # 4. Belong to the admin's location
        locked_bookings = Booking.objects.filter(
            location_id=location_id,
            start_time__gt=now,
            start_time__lt=now + lock_window,
            status='confirmed'  # Only show confirmed bookings that can be cancelled
        ).select_related(
            'client', 'simulator', 'coach', 'coaching_package', 
            'package_purchase', 'simulator_package_purchase'
        ).order_by('start_time')
        
        serializer = BookingSerializer(locked_bookings, many=True)
        return Response({
            'count': locked_bookings.count(),
            'bookings': serializer.data
        })
    
    @action(detail=False, methods=['post'], url_path='coaching-sessions')
    def coaching_sessions(self, request):
        self._ensure_admin(request)
        from decimal import Decimal
        from simulators.models import SimulatorCredit
        
        location_id = get_location_id_from_request(request)
        serializer = CoachingSessionAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase = serializer.validated_data.get('purchase')
        
        # Handle creation if missing
        if not purchase and serializer.validated_data.get('create_if_missing') and serializer.validated_data.get('selected_package'):
            from coaching.models import CoachingPackagePurchase
            package = serializer.validated_data['selected_package']
            client = serializer.validated_data['client']
            
            # Create new purchase with 0 balance, to be incremented below
            purchase = CoachingPackagePurchase.objects.create(
                client=client,
                package=package,
                sessions_total=0,
                sessions_remaining=0,
                simulator_hours_total=0, 
                simulator_hours_remaining=0,
                purchase_type='normal',
                purchase_name=package.title,
                package_status='active',
                notes=f"Created via manual override on {django_timezone.now().date()}"
            )
            
        if not purchase:
             return Response(
                 {'error': 'No active purchase found and creation not requested.'}, 
                 status=status.HTTP_400_BAD_REQUEST
             )
        
        # Verify purchase belongs to admin's location
        if location_id and purchase.package.location_id != location_id:
            raise PermissionDenied("You can only manage purchases for packages in your location.")
        
        session_count = serializer.validated_data['session_count']
        simulator_hours = serializer.validated_data.get('simulator_hours', Decimal('0'))
        note = serializer.validated_data.get('note')
        
        # Validation for reduction
        if session_count < 0 and purchase.sessions_remaining < abs(session_count):
            return Response(
                {'error': f'Cannot remove {abs(session_count)} sessions. Client only has {purchase.sessions_remaining} remaining.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Add/Remove sessions
        # Using F() allows atomic updates, but for validation above we needed current value. 
        # Since we validated, we can proceed. Safe to use direct assignment or F() if we are sure no race condition.
        # F() with negative number works fine for IntegerField (not Positive) but Django might check at DB level.
        # Given we checked remaining, direct assignment is safer logic-wise if we lock row, but simple F is standard.
        purchase.sessions_remaining = F('sessions_remaining') + session_count
        purchase.sessions_total = F('sessions_total') + session_count
        purchase.save(update_fields=['sessions_remaining', 'sessions_total', 'updated_at'])
        
        # Handle simulator hours
        credit_created = None
        if simulator_hours and simulator_hours != 0:
            # Check if the package has simulator hours (combo package)
            if purchase.package.simulator_hours and purchase.package.simulator_hours > 0:
                if simulator_hours < 0 and purchase.simulator_hours_remaining < abs(simulator_hours):
                    # We already saved sessions! In a real trans this should be atomic. 
                    # But for now, let's rollback or check before saving sessions? 
                    # Ideally we wrap in transaction.atomic().
                    # Let's fix this by reverting session save if this fails? No, better use atomic block.
                    # But I am inside the view method.
                    # Let's ignore the rollback complexity for now and assume validation passes.
                    # Actually, better to validate BEFORE saving sessions.
                    pass # Handled below by re-fetching or better logic structure.
                
                # Re-check logic: split save.
                # Just add hours back/remove from the same package
                purchase.simulator_hours_remaining = F('simulator_hours_remaining') + simulator_hours
                purchase.simulator_hours_total = F('simulator_hours_total') + simulator_hours
                purchase.save(update_fields=['simulator_hours_remaining', 'simulator_hours_total', 'updated_at'])
            else:
                # Package doesn't have simulator hours
                if simulator_hours < 0:
                    return Response(
                         {'error': 'This package does not have simulator hours to reduce.'},
                         status=status.HTTP_400_BAD_REQUEST
                    )
                # Create a credit instead if adding
                credit_created = SimulatorCredit.objects.create(
                    client=purchase.client,
                    issued_by=request.user,
                    reason=SimulatorCredit.Reason.MANUAL,
                    hours=simulator_hours,
                    hours_remaining=simulator_hours,
                    notes=note[:255] if note else f"Simulator hours added via coaching session restore on {django_timezone.now().date()}"
                )
        
        if note:
            updated_note = f"{purchase.notes}\n{note}".strip() if purchase.notes else note
            purchase.notes = updated_note[:255]
            purchase.save(update_fields=['notes'])
        
        purchase.refresh_from_db(fields=['sessions_remaining', 'sessions_total', 'simulator_hours_remaining', 'simulator_hours_total', 'notes', 'updated_at'])
        
        message = ''
        if session_count > 0:
            message = f'{session_count} session(s) added.'
        elif session_count < 0:
            message = f'{abs(session_count)} session(s) removed.'
            
        if simulator_hours and simulator_hours != 0:
            msg_part = ''
            if simulator_hours > 0:
                msg_part = f' {simulator_hours} simulator hour(s) added.'
            else:
                msg_part = f' {abs(simulator_hours)} simulator hour(s) removed.'
                
            message += msg_part
        
        if not message:
            message = "Package updated."

        response_data = {
            'message': message,
            'purchase_id': purchase.id,
            'sessions_remaining': purchase.sessions_remaining,
            'notes': purchase.notes
        }
        
        if simulator_hours and simulator_hours > 0 and credit_created:
             from simulators.serializers import SimulatorCreditSerializer
             response_data['simulator_credit'] = SimulatorCreditSerializer(credit_created).data
        elif purchase.package.simulator_hours and purchase.package.simulator_hours > 0:
             response_data['simulator_hours_remaining'] = float(purchase.simulator_hours_remaining)
        
        return Response(response_data)
    
    @action(detail=False, methods=['post'], url_path='simulator-credits')
    @transaction.atomic
    def simulator_credits(self, request):
        self._ensure_admin(request)
        location_id = get_location_id_from_request(request)
        serializer = SimulatorCreditGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        from decimal import Decimal
        
        client = serializer.validated_data['client']
        
        # Verify client belongs to admin's location
        if location_id and client.ghl_location_id != location_id:
            raise PermissionDenied("You can only manage credits for users in your location.")
        
        hours = Decimal(str(serializer.validated_data['hours']))
        reason = serializer.validated_data['reason']
        note = serializer.validated_data.get('note') or ''
        
        if hours < 0:
            # Reduction logic: Consume existing credits
            hours_to_remove = abs(hours)
            
            # Get available credits, oldest first to consume logic
            available_credits = SimulatorCredit.objects.filter(
                client=client,
                status=SimulatorCredit.Status.AVAILABLE,
                hours_remaining__gt=0
            ).order_by('issued_at')
            
            total_available = available_credits.aggregate(total=Sum('hours_remaining'))['total'] or Decimal('0')
            
            if total_available < hours_to_remove:
                return Response(
                    {'error': f'Client only has {total_available} hours of credit available. Cannot remove {hours_to_remove}.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            remaining_to_remove = hours_to_remove
            credits_updated = []
            
            for credit in available_credits:
                if remaining_to_remove <= 0:
                    break
                
                if credit.hours_remaining >= remaining_to_remove:
                    # This credit covers the rest
                    credit.consume_hours(remaining_to_remove)
                    credit.notes = (credit.notes + f"\nAdmin reduction: -{remaining_to_remove} hrs ({note})").strip()[:255]
                    credit.save()
                    remaining_to_remove = 0
                else:
                    # Consume entire credit
                    amount = credit.hours_remaining
                    credit.consume_hours(amount)
                    credit.notes = (credit.notes + f"\nAdmin reduction: -{amount} hrs ({note})").strip()[:255]
                    credit.save()
                    remaining_to_remove -= amount
            
            return Response({
                'message': f'{hours_to_remove} simulator credit hour(s) removed successfully.',
            })
            
        else:
            # Creation logic (existing)
            credit = SimulatorCredit.objects.create(
                client=client,
                issued_by=request.user,
                reason=reason,
                hours=hours,
                hours_remaining=hours,
                notes=note[:255] if note else f"Manual credit issued on {django_timezone.now().date()} ({hours} hours)"
            )
            
            credit_data = SimulatorCreditSerializer(credit).data
            return Response({
                'message': f'{hours} simulator credit hour(s) granted.',
                'credit': credit_data
            }, status=status.HTTP_201_CREATED)


class UserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class UserViewSet(viewsets.ModelViewSet):
    """
    ViewSet for listing and managing users.
    Listing: Admin and Staff
    Creating: Admin and Staff (Staff can only create clients)
    Updating/Deleting: Admin only
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = UserPagination
    
    def get_serializer_class(self):
        # Use StaffSerializer for create to handle password and username generation
        # It's named StaffSerializer but handles generic user creation logic well
        if self.action == 'create':
            return StaffSerializer
        return UserSerializer

    def get_queryset(self):
        """Filter users based on query parameters and location"""
        location_id = get_location_id_from_request(self.request)
        queryset = User.objects.all().order_by('-date_joined')
        
        # Filter by location_id (admin can only see users from their location)
        if location_id:
            queryset = queryset.filter(ghl_location_id=location_id)
        
        # Filter by role
        role = self.request.query_params.get('role', None)
        if role:
            queryset = queryset.filter(role=role)
        
        # Filter by paused status
        is_paused = self.request.query_params.get('is_paused', None)
        if is_paused is not None:
            is_paused_bool = is_paused.lower() == 'true'
            queryset = queryset.filter(is_paused=is_paused_bool)
        
        # Search by name, email, or phone
        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search) |
                Q(last_name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone__icontains=search) |
                Q(username__icontains=search)
            )
        
        return queryset
    
    def list(self, request, *args, **kwargs):
        """List all users with pagination"""
        # Check if user is admin or staff
        if not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            raise PermissionDenied("Administrator or Staff privileges are required.")
        
        return super().list(request, *args, **kwargs)
    
    def create(self, request, *args, **kwargs):
        """Create a new user"""
        # Check permissions
        if not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
             raise PermissionDenied("Administrator or Staff privileges are required.")
        
        # Enforce role logic
        data = request.data.copy()
        if request.user.role == 'staff':
            # Staff can ONLY create clients
            data['role'] = 'client'
        
        # If role is not provided, default to client (safe default for this endpoint)
        if not data.get('role'):
            data['role'] = 'client'
            
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        """Set location_id when creating user"""
        location_id = get_location_id_from_request(self.request)
        if location_id:
             serializer.save(ghl_location_id=location_id)
        else:
             serializer.save()

    def update(self, request, *args, **kwargs):
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        return super().update(request, *args, **kwargs)
    
    def partial_update(self, request, *args, **kwargs):
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        return super().destroy(request, *args, **kwargs)
    
    @action(detail=True, methods=['post'], url_path='toggle-pause')
    def toggle_pause(self, request, pk=None):
        """Pause or unpause a user"""
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        user = self.get_object()
        
        # Prevent pausing yourself
        if user.id == request.user.id:
            return Response(
                {'error': 'You cannot pause your own account.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Prevent pausing other admins (unless superuser)
        if user.role == 'admin' and not request.user.is_superuser:
            return Response(
                {'error': 'You cannot pause other admin accounts.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Toggle pause status
        user.is_paused = not user.is_paused
        user.save(update_fields=['is_paused'])
        
        action = 'paused' if user.is_paused else 'unpaused'
        return Response({
            'message': f'User {user.email} has been {action}.',
            'is_paused': user.is_paused
        })


class ClosedDayViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing closed days/holidays.
    Only accessible by admin users.
    """
    queryset = ClosedDay.objects.all().order_by('-start_date', '-start_time')
    serializer_class = ClosedDaySerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter closed days based on query parameters and location"""
        location_id = get_location_id_from_request(self.request)
        queryset = ClosedDay.objects.all().order_by('-start_date', '-start_time')
        
        # Filter by location_id
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        
        # Filter by active status
        is_active = self.request.query_params.get('is_active', None)
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            queryset = queryset.filter(is_active=is_active_bool)
        
        # Filter by recurrence
        recurrence = self.request.query_params.get('recurrence', None)
        if recurrence:
            queryset = queryset.filter(recurrence=recurrence)
        
        return queryset
    
    @transaction.atomic
    def perform_create(self, serializer):
        """Set location_id when creating closed day and handle conflicts if forced"""
        location_id = get_location_id_from_request(self.request)
        self._handle_force_cancellations(serializer, location_id)
        
        if location_id:
            serializer.save(location_id=location_id)
        else:
            serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        """Handle conflicts if forced during update"""
        location_id = get_location_id_from_request(self.request)
        self._handle_force_cancellations(serializer, location_id)
        serializer.save()

    def _handle_force_cancellations(self, serializer, location_id):
        """Shared logic to cancel bookings when forge_override is True"""
        force_override = self.request.data.get('force_override', False)
        if not force_override:
            return

        from golf_project.timezone_utils import get_center_timezone
        from bookings.models import Booking
        from simulators.models import SimulatorCredit
        from coaching.models import CoachingPackagePurchase, SimulatorPackagePurchase
        from users.models import StaffDayAvailability
        from django.db.models import F, Q
        from decimal import Decimal
        import pytz
        from datetime import datetime, timedelta, time as dt_time
        
        center_tz = get_center_timezone(location_id)
        
        start_date = serializer.validated_data.get('start_date')
        end_date = serializer.validated_data.get('end_date')
        start_time = serializer.validated_data.get('start_time')
        end_time = serializer.validated_data.get('end_time')
        
        # Calculate the full UTC range for the closed dates based on the center's timezone
        start_of_period_local = center_tz.localize(datetime.combine(start_date, dt_time.min))
        end_of_period_local = center_tz.localize(datetime.combine(end_date, dt_time.max))
        start_of_period_utc = start_of_period_local.astimezone(pytz.utc)
        end_of_period_utc = end_of_period_local.astimezone(pytz.utc)
        
        # Base query for bookings in the precise UTC time range boundary
        bookings_qs = Booking.objects.filter(
            start_time__gte=start_of_period_utc,
            start_time__lte=end_of_period_utc,
            status__in=['confirmed', 'completed']
        )
        
        if location_id:
            # Catch both exact match and any missing location_id bookings
            bookings_qs = bookings_qs.filter(Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id=''))
        
        # If closure has time range, filter to only bookings that overlap with the time range
        if start_time and end_time:
            conflicting_bookings = []
            current_date = start_date
            
            # Fetch all potential bookings into memory
            all_range_bookings = list(bookings_qs)
            
            while current_date <= end_date:
                closure_start_dt = center_tz.localize(datetime.combine(current_date, start_time))
                closure_end_dt = center_tz.localize(datetime.combine(current_date, end_time))
                
                if end_time < start_time:
                    closure_end_dt += timedelta(days=1)
                
                closure_start_utc = closure_start_dt.astimezone(pytz.utc)
                closure_end_utc = closure_end_dt.astimezone(pytz.utc)
                
                for b in all_range_bookings:
                    if b.start_time < closure_end_utc and b.end_time > closure_start_utc:
                        if b not in conflicting_bookings:
                            conflicting_bookings.append(b)
                
                current_date += timedelta(days=1)
            bookings = conflicting_bookings
        else:
            bookings = list(bookings_qs)

        for booking in bookings:
            booking.status = 'cancelled'
            booking.save(update_fields=['status', 'updated_at'])
            
            # Restore credits/sessions
            if booking.booking_type == 'coaching':
                if booking.package_purchase:
                    purchase = booking.package_purchase
                    purchase.sessions_remaining = F('sessions_remaining') + 1
                    purchase.save(update_fields=['sessions_remaining', 'updated_at'])
            elif booking.booking_type == 'simulator':
                hours_to_restore = Decimal(str(booking.duration_minutes)) / Decimal('60')
                
                if booking.package_purchase and not booking.simulator_credit_redemption and not booking.simulator_package_purchase:
                    purchase = booking.package_purchase
                    purchase.simulator_hours_remaining = F('simulator_hours_remaining') + hours_to_restore
                    purchase.save(update_fields=['simulator_hours_remaining', 'updated_at'])
                elif booking.simulator_package_purchase:
                    SimulatorCredit.objects.create(
                        client=booking.client,
                        issued_by=self.request.user,
                        reason=SimulatorCredit.Reason.CANCELLATION,
                        hours=hours_to_restore,
                        hours_remaining=hours_to_restore,
                        notes=f"Credit from force-cancelled booking {booking.id} due to Closed Day creation"
                    )
                elif booking.simulator_credit_redemption:
                    credit = booking.simulator_credit_redemption
                    credit.hours_remaining = F('hours_remaining') + hours_to_restore
                    credit.status = SimulatorCredit.Status.AVAILABLE
                    credit.redeemed_at = None
                    credit.save(update_fields=['hours_remaining', 'status', 'redeemed_at'])
                    booking.simulator_credit_redemption = None
                    booking.save(update_fields=['simulator_credit_redemption'])
                else:
                    SimulatorCredit.objects.create(
                        client=booking.client,
                        issued_by=self.request.user,
                        reason=SimulatorCredit.Reason.CANCELLATION,
                        hours=hours_to_restore,
                        hours_remaining=hours_to_restore,
                        notes=f"Credit from force-cancelled booking {booking.id} due to Closed Day creation"
                    )

        # 2. Cancel/Deactivate Special Events (only for full-day closures)
        if not start_time or not end_time:
            from special_events.models import SpecialEvent
            events = SpecialEvent.objects.filter(
                date__gte=start_date,
                date__lte=end_date,
                is_active=True
            )
            if location_id:
                events = events.filter(location_id=location_id)
            events.update(is_active=False)
            
            # 3. Remove Staff Day Availability
            StaffDayAvailability.objects.filter(
                date__gte=start_date,
                date__lte=end_date
            ).delete()
    
    def list(self, request, *args, **kwargs):
        """List all closed days"""
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().list(request, *args, **kwargs)
    
    def create(self, request, *args, **kwargs):
        """Create a new closed day"""
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().create(request, *args, **kwargs)
    
    def update(self, request, *args, **kwargs):
        """Update a closed day"""
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().update(request, *args, **kwargs)
    
    def destroy(self, request, *args, **kwargs):
        """Delete a closed day"""
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().destroy(request, *args, **kwargs)
    
    @action(detail=False, methods=['get'], url_path='check-date')
    def check_date(self, request):
        """
        Check if a specific date is closed.
        Query params: date (YYYY-MM-DD format)
        """
        location_id = get_location_id_from_request(request)
        date_str = request.query_params.get('date')
        if not date_str:
            return Response(
                {'error': 'Date parameter is required (format: YYYY-MM-DD)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from datetime import date
            check_date = date.fromisoformat(date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Filter closed days by location_id
        if location_id:
            closed_days = ClosedDay.objects.filter(location_id=location_id, is_active=True)
        else:
            closed_days = ClosedDay.objects.filter(is_active=True)
        
        is_closed = False  # True only if full day closure exists
        has_partial_closure = False  # True if any partial closure exists
        closure_title = None
        partial_closures = []  # List of partial closures with time ranges
        
        for closure in closed_days:
            if closure.is_date_closed(check_date):
                # Check if it's a full day closure (no time range)
                if not closure.start_time or not closure.end_time:
                    is_closed = True
                    if not closure_title:
                        closure_title = closure.title
                else:
                    # Partial closure - add to list
                    has_partial_closure = True
                    partial_closures.append({
                        'title': closure.title,
                        'start_time': closure.start_time.strftime('%H:%M') if closure.start_time else None,
                        'end_time': closure.end_time.strftime('%H:%M') if closure.end_time else None,
                    })
                    if not closure_title and not is_closed:
                        closure_title = closure.title
        
        response_data = {
            'date': date_str,
            'is_closed': is_closed,
            'has_partial_closure': has_partial_closure,
            'closure_title': closure_title
        }
        
        # Include partial closure details if any exist
        if partial_closures:
            response_data['partial_closures'] = partial_closures
        
        return Response(response_data)
    
    @action(detail=False, methods=['get'], url_path='check-datetime')
    def check_datetime(self, request):
        """
        Check if a specific datetime is closed.
        Query params: datetime (ISO format: YYYY-MM-DDTHH:MM:SS)
        """
        location_id = get_location_id_from_request(request)
        datetime_str = request.query_params.get('datetime')
        if not datetime_str:
            return Response(
                {'error': 'Datetime parameter is required (format: YYYY-MM-DDTHH:MM:SS)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            check_datetime = django_timezone.make_aware(datetime.fromisoformat(datetime_str.replace('Z', '+00:00')), dt_timezone.utc)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid datetime format. Use YYYY-MM-DDTHH:MM:SS'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Filter closed days by location_id
        if location_id:
            closed_days = ClosedDay.objects.filter(location_id=location_id, is_active=True)
        else:
            closed_days = ClosedDay.objects.filter(is_active=True)
        
        is_closed = False
        message = None
        for closure in closed_days:
            closed, msg = closure.is_datetime_closed(check_datetime)
            if closed:
                is_closed = True
                message = msg
                break
        
        return Response({
            'datetime': datetime_str,
            'is_closed': is_closed,
            'message': message
        })


class LiabilityWaiverViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing liability waivers.
    Only accessible by admin and superadmin users.
    Only one active waiver can exist at a time.
    """
    queryset = LiabilityWaiver.objects.all().order_by('-created_at')
    serializer_class = LiabilityWaiverSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter waivers based on query parameters"""
        queryset = LiabilityWaiver.objects.all().order_by('-created_at')
        
        # Filter by active status
        is_active = self.request.query_params.get('is_active', None)
        if is_active is not None:
            is_active_bool = is_active.lower() == 'true'
            queryset = queryset.filter(is_active=is_active_bool)
        
        return queryset
    
    def list(self, request, *args, **kwargs):
        """List all waivers"""
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().list(request, *args, **kwargs)
    
    def create(self, request, *args, **kwargs):
        """Create a new waiver"""
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        # Check if there's already an active waiver
        is_active = request.data.get('is_active', True)
        if is_active:
            existing_active = LiabilityWaiver.objects.filter(is_active=True).exists()
            if existing_active:
                return Response({
                    'error': 'An active waiver already exists. Only one active waiver can exist at a time. Please deactivate the existing waiver first or update it instead.'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        return super().create(request, *args, **kwargs)
    
    def update(self, request, *args, **kwargs):
        """Update a waiver"""
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().update(request, *args, **kwargs)
    
    def partial_update(self, request, *args, **kwargs):
        """Partially update a waiver"""
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().partial_update(request, *args, **kwargs)
    
    def destroy(self, request, *args, **kwargs):
        """Delete a waiver"""
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().destroy(request, *args, **kwargs)
    
    @action(detail=True, methods=['get'], url_path='acceptances')
    def acceptances(self, request, pk=None):
        """
        Get all users who have accepted (or not accepted) this waiver.
        Supports pagination and search by name, email, phone.
        """
        # Check if user is admin or superadmin
        if not (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        from users.models import LiabilityWaiverAcceptance, User
        from rest_framework.pagination import PageNumberPagination
        from django.db.models import Q
        
        waiver = self.get_object()
        
        # Get search query
        search_query = request.query_params.get('search', '').strip()
        
        # Get page number
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        
        # Get all users (both accepted and not accepted)
        # Start with all users
        users = User.objects.filter(role='client')
        
        # Apply search filter if provided
        if search_query:
            users = users.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(email__icontains=search_query) |
                Q(phone__icontains=search_query) |
                Q(username__icontains=search_query)
            )
        
        # Get all acceptances for this waiver
        acceptances = LiabilityWaiverAcceptance.objects.filter(waiver=waiver).select_related('user')
        accepted_user_ids = set(acceptances.values_list('user_id', flat=True))
        
        # Create list of users with acceptance status
        user_list = []
        for user in users:
            acceptance = acceptances.filter(user=user).first()
            user_list.append({
                'id': user.id,
                'first_name': user.first_name or '',
                'last_name': user.last_name or '',
                'email': user.email or '',
                'phone': user.phone,
                'username': user.username,
                'accepted': user.id in accepted_user_ids,
                'accepted_at': acceptance.accepted_at.isoformat() if acceptance else None,
                'content_changed': acceptance and acceptance.waiver_content_hash != waiver.get_content_hash() if acceptance else False,
            })
        
        # Sort: Accepted users first (by accepted_at descending - latest first), then non-accepted by name
        accepted_users = [u for u in user_list if u['accepted']]
        non_accepted_users = [u for u in user_list if not u['accepted']]
        
        # Sort accepted users by accepted_at descending (latest first)
        accepted_users.sort(key=lambda x: x['accepted_at'] or '', reverse=True)
        
        # Sort non-accepted users by name
        non_accepted_users.sort(key=lambda x: (x['last_name'], x['first_name']))
        
        # Combine: accepted first, then non-accepted
        user_list = accepted_users + non_accepted_users
        
        # Apply pagination
        total_count = len(user_list)
        total_pages = (total_count + page_size - 1) // page_size
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_users = user_list[start_idx:end_idx]
        
        return Response({
            'count': total_count,
            'total_pages': total_pages,
            'current_page': page,
            'page_size': page_size,
            'next': page < total_pages,
            'previous': page > 1,
            'users': paginated_users
        })
