from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from django.db.models import Count, Sum, Q, F
from django.utils import timezone
from datetime import datetime, timedelta
from users.models import User, StaffAvailability
from simulators.models import Simulator, SimulatorCredit
from bookings.models import Booking
from users.serializers import UserSerializer, StaffSerializer, StaffAvailabilitySerializer
from bookings.serializers import BookingSerializer
from .serializers import CoachingSessionAdjustmentSerializer, SimulatorCreditGrantSerializer
from simulators.serializers import SimulatorCreditSerializer

class AdminDashboardViewSet(viewsets.ViewSet):
    @action(detail=False, methods=['get'])
    def stats(self, request):
        today = timezone.now().date()
        
        stats = {
            'total_bookings': Booking.objects.count(),
            'today_bookings': Booking.objects.filter(
                start_time__date=today
            ).count(),
            'active_simulators': Simulator.objects.filter(is_active=True).count(),
            'total_revenue': Booking.objects.aggregate(
                total=Sum('total_price')
            )['total'] or 0
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['get'], url_path='recent-bookings')
    def recent_bookings(self, request):
        bookings = Booking.objects.select_related(
            'client', 'simulator', 'coach', 'coaching_package'
        ).order_by('-created_at')[:10]
        
        from bookings.serializers import BookingSerializer
        serializer = BookingSerializer(bookings, many=True)
        return Response(serializer.data)

class StaffViewSet(viewsets.ModelViewSet):
    queryset = User.objects.filter(role__in=['staff', 'admin'])
    serializer_class = StaffSerializer
    
    def get_serializer_class(self):
        # Use UserSerializer for read operations to include username
        if self.action in ['list', 'retrieve']:
            return UserSerializer
        # Use StaffSerializer for create/update to auto-generate username
        return StaffSerializer
    
    @action(detail=True, methods=['get', 'put'])
    def availability(self, request, pk=None):
        staff = self.get_object()
        
        if request.method == 'GET':
            # Get all recurring weekly availability
            availability = StaffAvailability.objects.filter(staff=staff).order_by('day_of_week', 'start_time')
            serializer = StaffAvailabilitySerializer(availability, many=True)
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
                        serializer = StaffAvailabilitySerializer(data=serializer_data)
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
            serializer = StaffAvailabilitySerializer(updated_availability, many=True)
            return Response(serializer.data)


class AdminOverrideViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    
    def _ensure_admin(self, request):
        if getattr(request.user, 'role', None) != 'admin' and not getattr(request.user, 'is_superuser', False):
            raise PermissionDenied("Administrator privileges are required for this action.")
    
    @action(detail=False, methods=['post'], url_path='coaching-sessions')
    def coaching_sessions(self, request):
        self._ensure_admin(request)
        serializer = CoachingSessionAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase = serializer.validated_data['purchase']
        session_count = serializer.validated_data['session_count']
        note = serializer.validated_data.get('note')
        
        purchase.sessions_remaining = F('sessions_remaining') + session_count
        purchase.save(update_fields=['sessions_remaining', 'updated_at'])
        if note:
            updated_note = f"{purchase.notes}\n{note}".strip() if purchase.notes else note
            purchase.notes = updated_note[:255]
            purchase.save(update_fields=['notes'])
        purchase.refresh_from_db(fields=['sessions_remaining', 'notes', 'updated_at'])
        
        return Response({
            'message': f'{session_count} session(s) added back to the package.',
            'purchase_id': purchase.id,
            'sessions_remaining': purchase.sessions_remaining,
            'notes': purchase.notes
        })
    
    @action(detail=False, methods=['post'], url_path='simulator-credits')
    def simulator_credits(self, request):
        self._ensure_admin(request)
        serializer = SimulatorCreditGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        client = serializer.validated_data['client']
        token_count = serializer.validated_data['token_count']
        reason = serializer.validated_data['reason']
        note = serializer.validated_data.get('note') or ''
        
        credits = []
        for _ in range(token_count):
            credit = SimulatorCredit.objects.create(
                client=client,
                issued_by=request.user,
                reason=reason,
                notes=note[:255] if note else f"Manual credit issued on {timezone.now().date()}"
            )
            credits.append(credit)
        
        credit_data = SimulatorCreditSerializer(credits, many=True).data
        return Response({
            'message': f'{token_count} simulator credit(s) granted.',
            'credits': credit_data
        }, status=status.HTTP_201_CREATED)


class UserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for listing all users with pagination.
    Only accessible by admin users.
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = UserPagination
    
    def get_queryset(self):
        """Filter users based on query parameters"""
        queryset = User.objects.all().order_by('-date_joined')
        
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
        # Check if user is admin
        if not (request.user.role == 'admin' or request.user.is_superuser):
            raise PermissionDenied("Administrator privileges are required.")
        
        return super().list(request, *args, **kwargs)
    
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
