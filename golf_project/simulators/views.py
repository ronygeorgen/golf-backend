from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from django.utils import timezone
from datetime import datetime
from .models import Simulator, DurationPrice, SimulatorAvailability, SimulatorCredit
from .serializers import (
    SimulatorSerializer,
    DurationPriceSerializer,
    SimulatorAvailabilitySerializer,
    SimulatorCreditSerializer
)

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
    
    def get_queryset(self):
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        queryset = Simulator.objects.all().order_by('bay_number')
        
        # Filter by location_id
        if location_id:
            queryset = queryset.filter(location_id=location_id)
        
        return queryset
    
    def perform_create(self, serializer):
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        
        # Check if bay number already exists for this location
        bay_number = serializer.validated_data.get('bay_number')
        if location_id:
            if Simulator.objects.filter(bay_number=bay_number, location_id=location_id).exists():
                from rest_framework.exceptions import ValidationError
                raise ValidationError({'bay_number': [f'Bay number {bay_number} already exists for this location']})
            serializer.save(location_id=location_id)
        else:
            # Fallback: check globally if no location_id
            if Simulator.objects.filter(bay_number=bay_number).exists():
                from rest_framework.exceptions import ValidationError
                raise ValidationError({'bay_number': [f'Bay number {bay_number} already exists']})
            serializer.save()
    
    def update(self, request, *args, **kwargs):
        """Override update to handle partial updates even for PUT requests"""
        partial = kwargs.pop('partial', False)
        # If it's a PUT request but not all fields are provided, treat it as partial
        if request.method == 'PUT' and not partial:
            # Check if all required fields are in the request data
            required_fields = ['name', 'bay_number']
            has_all_required = all(field in request.data for field in required_fields)
            if not has_all_required:
                partial = True
                kwargs['partial'] = True
        
        # Call parent update method with partial flag
        return super().update(request, *args, **kwargs)
    
    def perform_update(self, serializer):
        """Handle partial updates - allow updating only specific fields like is_active"""
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        
        # For partial updates, only validate fields that are being updated
        instance = self.get_object()
        
        # If bay_number is being updated, check for duplicates
        if 'bay_number' in serializer.validated_data:
            bay_number = serializer.validated_data.get('bay_number')
            if location_id:
                existing = Simulator.objects.filter(bay_number=bay_number, location_id=location_id).exclude(pk=instance.pk)
                if existing.exists():
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError({'bay_number': [f'Bay number {bay_number} already exists for this location']})
            else:
                existing = Simulator.objects.filter(bay_number=bay_number).exclude(pk=instance.pk)
                if existing.exists():
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError({'bay_number': [f'Bay number {bay_number} already exists']})
        
        # Save with location_id if provided
        if location_id:
            serializer.save(location_id=location_id)
        else:
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
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(request)
        active_simulators = Simulator.objects.filter(is_active=True)
        
        if location_id:
            active_simulators = active_simulators.filter(location_id=location_id)
        
        serializer = self.get_serializer(active_simulators, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get', 'put'], url_path='availability')
    def availability(self, request, pk=None):
        """Get or update simulator availability"""
        from users.utils import get_location_id_from_request
        simulator = self.get_object()
        
        # Verify simulator belongs to admin's location
        location_id = get_location_id_from_request(request)
        if location_id and simulator.location_id != location_id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You can only manage availability for simulators in your location.")
        
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
            
            # Get ALL existing availability entries for this simulator
            all_existing = SimulatorAvailability.objects.filter(simulator=simulator)
            
            # Build sets of entries to keep (by ID and by day_of_week + start_time)
            ids_to_keep = set()
            entries_to_keep_by_key = set()
            
            for avail_data in availability_data:
                # If entry has an ID, keep it by ID
                entry_id = avail_data.get('id')
                if entry_id:
                    ids_to_keep.add(int(entry_id))
                
                # Also track by day_of_week + start_time for entries without IDs (new entries)
                day_of_week = avail_data.get('day_of_week')
                start_time_str = avail_data.get('start_time')
                if day_of_week is not None and start_time_str:
                    # Normalize start_time format (HH:MM)
                    if ':' in start_time_str:
                        start_time_normalized = start_time_str[:5] if len(start_time_str) >= 5 else start_time_str
                        entries_to_keep_by_key.add((int(day_of_week), start_time_normalized))
            
            # Delete entries that are not in the keep list
            # An entry is kept if:
            # 1. Its ID is in ids_to_keep, OR
            # 2. Its (day_of_week, start_time) matches an entry in entries_to_keep_by_key
            to_delete = []
            for existing_entry in all_existing:
                entry_id = existing_entry.id
                day_of_week = existing_entry.day_of_week
                start_time_str = str(existing_entry.start_time)[:5]  # Format as HH:MM
                entry_key = (day_of_week, start_time_str)
                
                # Check if this entry should be kept
                should_keep = (
                    entry_id in ids_to_keep or
                    entry_key in entries_to_keep_by_key
                )
                
                if not should_keep:
                    to_delete.append(existing_entry.id)
            
            # Delete entries that are not in the keep list
            if to_delete:
                deleted_count = SimulatorAvailability.objects.filter(id__in=to_delete).delete()
                print(f"Deleted {deleted_count[0]} availability entries for simulator {simulator.id}")
            
            # Update or create each availability entry
            updated_availability = []
            for avail_data in availability_data:
                day_of_week = avail_data.get('day_of_week')
                if day_of_week is not None:
                    try:
                        day_of_week = int(day_of_week)
                        # Parse start_time
                        start_time_str = avail_data.get('start_time', '09:00')
                        try:
                            start_time_obj = datetime.strptime(start_time_str, '%H:%M').time()
                        except ValueError:
                            # Try with seconds if present
                            try:
                                start_time_obj = datetime.strptime(start_time_str, '%H:%M:%S').time()
                            except ValueError:
                                start_time_obj = datetime.strptime('09:00', '%H:%M').time()
                        
                        # Parse end_time
                        end_time_str = avail_data.get('end_time', '17:00')
                        try:
                            end_time_obj = datetime.strptime(end_time_str, '%H:%M').time()
                        except ValueError:
                            try:
                                end_time_obj = datetime.strptime(end_time_str, '%H:%M:%S').time()
                            except ValueError:
                                end_time_obj = datetime.strptime('17:00', '%H:%M').time()
                        
                        # Use update_or_create directly to handle uniqueness constraint properly
                        # This avoids serializer validation issues with unique constraints
                        availability, created = SimulatorAvailability.objects.update_or_create(
                            simulator=simulator,
                            day_of_week=day_of_week,
                            start_time=start_time_obj,
                            defaults={
                                'end_time': end_time_obj,
                            }
                        )
                        updated_availability.append(availability)
                    except (ValueError, TypeError) as e:
                        print(f"Error processing availability data: {e}")
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


class SimulatorCreditViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SimulatorCreditSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        queryset = SimulatorCredit.objects.select_related('client', 'source_booking').order_by('-issued_at')
        
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        if getattr(user, 'role', None) in ['admin', 'staff'] or getattr(user, 'is_superuser', False):
            client_id = self.request.query_params.get('client_id')
            if client_id:
                return queryset.filter(client_id=client_id)
        
        return queryset.filter(client=user)