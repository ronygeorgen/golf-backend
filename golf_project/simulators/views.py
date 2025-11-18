from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from django.utils import timezone
from datetime import datetime
from .models import Simulator, DurationPrice, SimulatorAvailability
from .serializers import SimulatorSerializer, DurationPriceSerializer, SimulatorAvailabilitySerializer

class SimulatorViewSet(viewsets.ModelViewSet):
    queryset = Simulator.objects.all().order_by('bay_number')
    serializer_class = SimulatorSerializer
    
    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve', 'active_simulators']:
            permission_classes = [AllowAny]  # Public access for viewing simulators
        else:
            permission_classes = [IsAuthenticated, IsAdminUser]  # Admin only for create/update/delete
        return [permission() for permission in permission_classes]
    
    def perform_create(self, serializer):
        # Check if bay number already exists
        bay_number = serializer.validated_data.get('bay_number')
        if Simulator.objects.filter(bay_number=bay_number).exists():
            return Response(
                {'error': f'Bay number {bay_number} already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )
        serializer.save()
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        simulator = self.get_object()
        simulator.is_active = not simulator.is_active
        simulator.save()
        return Response({
            'message': f'Simulator {"activated" if simulator.is_active else "deactivated"}',
            'is_active': simulator.is_active
        })
    
    @action(detail=False, methods=['get'])
    def active_simulators(self, request):
        active_simulators = Simulator.objects.filter(is_active=True)
        serializer = self.get_serializer(active_simulators, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get', 'put'], url_path='availability')
    def availability(self, request, pk=None):
        """Get or update simulator availability"""
        simulator = self.get_object()
        
        if request.method == 'GET':
            # Get all recurring weekly availability
            availability = SimulatorAvailability.objects.filter(
                simulator=simulator
            ).order_by('day_of_week', 'start_time')
            serializer = SimulatorAvailabilitySerializer(availability, many=True)
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
                existing_for_days = SimulatorAvailability.objects.filter(
                    simulator=simulator,
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
                print(f"Deleted {deleted_count[0]} availability entries for simulator {simulator.id}")
            
            # Update or create each availability entry
            updated_availability = []
            for avail_data in availability_data:
                day_of_week = avail_data.get('day_of_week')
                if day_of_week is not None:
                    try:
                        day_of_week = int(day_of_week)
                        # Use serializer to handle timezone conversion
                        serializer_data = {**avail_data, 'simulator': simulator.id, 'day_of_week': day_of_week}
                        serializer = SimulatorAvailabilitySerializer(data=serializer_data)
                        if serializer.is_valid():
                            availability, created = SimulatorAvailability.objects.update_or_create(
                                simulator=simulator,
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
                                availability, created = SimulatorAvailability.objects.update_or_create(
                                    simulator=simulator,
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
            serializer = SimulatorAvailabilitySerializer(updated_availability, many=True)
            return Response(serializer.data)

class DurationPriceViewSet(viewsets.ModelViewSet):
    queryset = DurationPrice.objects.all().order_by('duration_minutes')
    serializer_class = DurationPriceSerializer
    
    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve']:
            permission_classes = [AllowAny]  # Public access for viewing prices
        else:
            permission_classes = [IsAuthenticated, IsAdminUser]  # Admin only for create/update/delete
        return [permission() for permission in permission_classes]
    
    def perform_create(self, serializer):
        duration = serializer.validated_data.get('duration_minutes')
        if DurationPrice.objects.filter(duration_minutes=duration).exists():
            return Response(
                {'error': f'Pricing for {duration} minutes already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )
        serializer.save()