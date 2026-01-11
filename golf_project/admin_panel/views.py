import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
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
from .serializers import CoachingSessionAdjustmentSerializer, SimulatorCreditGrantSerializer, ClosedDaySerializer
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
            
            # Get existing availability entries for the days in request
            requested_days = set()
            for avail_data in availability_data:
                day_of_week = avail_data.get('day_of_week')
                if day_of_week is not None:
                    requested_days.add(int(day_of_week))
            
            # Delete availability entries for these days that are not in the request
            if requested_days:
                existing_for_days = StaffAvailability.objects.filter(
                    staff=staff,
                    day_of_week__in=requested_days
                )
                # Get IDs of entries to keep
                entries_to_keep = set()
                for avail_data in availability_data:
                    day_of_week = avail_data.get('day_of_week')
                    start_time_str = avail_data.get('start_time')
                    if day_of_week is not None and start_time_str:
                        entries_to_keep.add((int(day_of_week), start_time_str))
                
                # Delete entries not in the keep list
                to_delete = existing_for_days.exclude(
                    id__in=[
                        av.id for av in existing_for_days 
                        if (av.day_of_week, str(av.start_time)[:5]) in entries_to_keep
                    ]
                )
                deleted_count = to_delete.delete()
                print(f"Deleted {deleted_count[0]} availability entries for staff {staff.id}")
            
            # Update or create each availability entry
            updated_availability = []
            for avail_data in availability_data:
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
            
            # Return updated availability list
            serializer = StaffAvailabilitySerializer(updated_availability, many=True, context={'location_id': location_id})
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
        if getattr(request.user, 'role', None) != 'admin' and not getattr(request.user, 'is_superuser', False):
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
        
        # Add sessions back
        purchase.sessions_remaining = F('sessions_remaining') + session_count
        purchase.save(update_fields=['sessions_remaining', 'updated_at'])
        
        # Handle simulator hours
        credit_created = None
        if simulator_hours and simulator_hours > 0:
            # Check if the package has simulator hours (combo package)
            if purchase.package.simulator_hours and purchase.package.simulator_hours > 0:
                # Add hours back to the same package
                purchase.simulator_hours_remaining = F('simulator_hours_remaining') + simulator_hours
                purchase.save(update_fields=['simulator_hours_remaining', 'updated_at'])
            else:
                # Package doesn't have simulator hours, create a credit instead
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
        
        purchase.refresh_from_db(fields=['sessions_remaining', 'simulator_hours_remaining', 'notes', 'updated_at'])
        
        message = f'{session_count} session(s) added back to the package.'
        if simulator_hours and simulator_hours > 0:
            if purchase.package.simulator_hours and purchase.package.simulator_hours > 0:
                message += f' {simulator_hours} simulator hour(s) added back to the package.'
            else:
                message += f' {simulator_hours} simulator hour(s) added as credit.'
        
        response_data = {
            'message': message,
            'purchase_id': purchase.id,
            'sessions_remaining': purchase.sessions_remaining,
            'notes': purchase.notes
        }
        
        if simulator_hours and simulator_hours > 0:
            if purchase.package.simulator_hours and purchase.package.simulator_hours > 0:
                response_data['simulator_hours_remaining'] = float(purchase.simulator_hours_remaining)
            else:
                from simulators.serializers import SimulatorCreditSerializer
                response_data['simulator_credit'] = SimulatorCreditSerializer(credit_created).data
        
        return Response(response_data)
    
    @action(detail=False, methods=['post'], url_path='simulator-credits')
    def simulator_credits(self, request):
        self._ensure_admin(request)
        location_id = get_location_id_from_request(request)
        serializer = SimulatorCreditGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        from decimal import Decimal
        
        client = serializer.validated_data['client']
        
        # Verify client belongs to admin's location
        if location_id and client.ghl_location_id != location_id:
            raise PermissionDenied("You can only grant credits to users in your location.")
        
        hours = Decimal(str(serializer.validated_data['hours']))
        reason = serializer.validated_data['reason']
        note = serializer.validated_data.get('note') or ''
        
        # Create a single credit with the specified hours
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
    
    def perform_create(self, serializer):
        """Set location_id when creating closed day"""
        location_id = get_location_id_from_request(self.request)
        if location_id:
            serializer.save(location_id=location_id)
        else:
            serializer.save()
    
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
        
        is_closed = False
        closure_title = None
        for closure in closed_days:
            if closure.is_date_closed(check_date):
                is_closed = True
                closure_title = closure.title
                break
        
        return Response({
            'date': date_str,
            'is_closed': is_closed,
            'closure_title': closure_title
        })
    
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
