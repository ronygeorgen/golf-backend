from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Count, Sum, Q
from django.utils import timezone
from datetime import datetime, timedelta
from users.models import User, StaffAvailability
from simulators.models import Simulator
from coaching.models import CoachingPackage
from bookings.models import Booking
from users.serializers import UserSerializer, StaffSerializer, StaffAvailabilitySerializer
from bookings.serializers import BookingSerializer

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
