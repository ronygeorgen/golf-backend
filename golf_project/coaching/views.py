from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from django.db.models import Q
from .models import CoachingPackage, CoachingPackagePurchase
from .serializers import CoachingPackageSerializer, CoachingPackagePurchaseSerializer

class CoachingPackageViewSet(viewsets.ModelViewSet):
    queryset = CoachingPackage.objects.all().order_by('-id')
    serializer_class = CoachingPackageSerializer
    
    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve', 'active_packages']:
            permission_classes = [AllowAny]  # Public access for viewing packages
        else:
            permission_classes = [IsAuthenticated, IsAdminUser]  # Admin only for create/update/delete
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        queryset = CoachingPackage.objects.all().order_by('-id')
        
        # Filter by active status if provided
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        # Filter by staff member
        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_members__id=staff_id)
        
        return queryset.select_related().prefetch_related('staff_members')
    
    def perform_create(self, serializer):
        package = serializer.save()
        
        # Log package creation
        print(f"New coaching package created: {package.title} by {self.request.user}")
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        package = self.get_object()
        package.is_active = not package.is_active
        package.save()
        return Response({
            'message': f'Package {"activated" if package.is_active else "deactivated"}',
            'is_active': package.is_active
        })
    
    @action(detail=True, methods=['post'])
    def assign_staff(self, request, pk=None):
        package = self.get_object()
        staff_ids = request.data.get('staff_ids', [])
        
        if not staff_ids:
            return Response(
                {'error': 'No staff IDs provided'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from users.models import User
        staff_members = User.objects.filter(id__in=staff_ids, role__in=['staff', 'admin'])
        package.staff_members.add(*staff_members)
        
        return Response({
            'message': f'Assigned {staff_members.count()} staff members to package'
        })
    
    @action(detail=True, methods=['post'])
    def remove_staff(self, request, pk=None):
        package = self.get_object()
        staff_ids = request.data.get('staff_ids', [])
        
        if not staff_ids:
            return Response(
                {'error': 'No staff IDs provided'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        package.staff_members.remove(*staff_ids)
        
        return Response({
            'message': f'Removed staff members from package'
        })
    
    @action(detail=False, methods=['get'])
    def active_packages(self, request):
        active_packages = CoachingPackage.objects.filter(is_active=True)
        serializer = self.get_serializer(active_packages, many=True)
        return Response(serializer.data)


class CoachingPackagePurchaseViewSet(viewsets.ModelViewSet):
    serializer_class = CoachingPackagePurchaseSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        base_qs = CoachingPackagePurchase.objects.select_related('client', 'package').prefetch_related('package__staff_members')
        
        if user.role in ['admin', 'staff']:
            return base_qs
        return base_qs.filter(client=user)
    
    def perform_create(self, serializer):
        package = serializer.validated_data.get('package')
        if not package or not package.is_active:
            raise serializers.ValidationError("Selected package is not available.")
        
        serializer.save(client=self.request.user)
    
    @action(detail=False, methods=['get'])
    def my(self, request):
        purchases = self.get_queryset().filter(client=request.user)
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)