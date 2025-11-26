import logging

from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ghl.services import (
    build_purchase_tags,
    purchase_custom_fields,
)

try:
    from ghl.tasks import sync_purchase_with_ghl_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    sync_purchase_with_ghl_task = None
from users.models import User
from .models import CoachingPackage, CoachingPackagePurchase, SessionTransfer
from .serializers import (
    CoachingPackageSerializer,
    CoachingPackagePurchaseSerializer,
    SessionTransferSerializer,
)

logger = logging.getLogger(__name__)

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
        base_qs = CoachingPackagePurchase.objects.select_related(
            'client', 'package', 'original_owner'
        ).prefetch_related('package__staff_members')
        
        if user.role in ['admin', 'staff']:
            return base_qs
        
        # Clients see their own purchases and gifts received
        return base_qs.filter(
            Q(client=user) | Q(recipient_phone=user.phone, gift_status='accepted')
        )
    
    def perform_create(self, serializer):
        package = serializer.validated_data.get('package')
        purchase_type = serializer.validated_data.get('purchase_type', 'normal')
        location_id = self.request.data.get('location_id')
        
        if not package or not package.is_active:
            raise serializers.ValidationError("Selected package is not available.")
        
        # For gift purchases, set client to recipient when creating
        if purchase_type == 'gift':
            recipient_phone = serializer.validated_data.get('recipient_phone')
            try:
                recipient = User.objects.get(phone=recipient_phone)
                purchase = serializer.save(client=recipient, original_owner=self.request.user)
            except User.DoesNotExist:
                raise serializers.ValidationError("Recipient not found.")
        else:
            purchase = serializer.save(client=self.request.user)

        if location_id and self.request.user.ghl_location_id != location_id:
            self.request.user.ghl_location_id = location_id
            self.request.user.save(update_fields=['ghl_location_id'])

        self._sync_purchase_with_ghl(purchase)
    
    @action(detail=False, methods=['get'])
    def my(self, request):
        purchases = self.get_queryset().filter(
            Q(client=request.user) | 
            Q(recipient_phone=request.user.phone, gift_status='accepted')
        )
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def gifts_pending(self, request):
        """Get gifts pending acceptance for current user"""
        pending_gifts = CoachingPackagePurchase.objects.filter(
            recipient_phone=request.user.phone,
            gift_status='pending',
            gift_expires_at__gt=timezone.now()
        )
        serializer = self.get_serializer(pending_gifts, many=True)
        return Response(serializer.data)

    def _sync_purchase_with_ghl(self, purchase):
        """
        Push purchase info into GHL so workflows can react to spend/tag updates.
        Uses Celery task for async processing if available, otherwise sync.
        """
        if not purchase:
            return

        contact_owner = purchase.original_owner or purchase.client
        if not contact_owner:
            return

        location_id = contact_owner.ghl_location_id or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
        if not location_id:
            return

        # Queue async task to sync purchase with GHL
        try:
            if CELERY_AVAILABLE and sync_purchase_with_ghl_task:
                sync_purchase_with_ghl_task.delay(purchase.id)
            else:
                # Fallback to synchronous call if Celery not available
                from ghl.services import sync_user_contact
                amount = getattr(purchase.package, 'price', 0)
                purchase_name = purchase.purchase_name or (purchase.package.title if purchase.package else 'Unknown')
                sync_user_contact(
                    contact_owner,
                    location_id=location_id,
                    tags=build_purchase_tags(amount),
                    custom_fields=purchase_custom_fields(purchase_name, amount),
                )
        except Exception as exc:
            logger.warning("Failed to sync GHL for purchase %s: %s", purchase.id, exc)


class GiftClaimView(APIView):
    """Handle gift claim (accept/reject)"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, token):
        purchase = get_object_or_404(
            CoachingPackagePurchase,
            gift_token=token,
            gift_status='pending'
        )
        
        # Verify recipient
        if purchase.recipient_phone != request.user.phone:
            return Response(
                {'error': 'You are not authorized to claim this gift.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Check expiration
        if purchase.gift_expires_at and purchase.gift_expires_at < timezone.now():
            purchase.gift_status = 'expired'
            purchase.save()
            return Response(
                {'error': 'This gift has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        action = request.data.get('action', 'accept')
        
        if action == 'accept':
            purchase.gift_status = 'accepted'
            purchase.package_status = 'active'
            purchase.client = request.user
            purchase.save()
            serializer = CoachingPackagePurchaseSerializer(purchase)
            return Response({
                'message': 'Gift accepted successfully.',
                'purchase': serializer.data
            })
        elif action == 'reject':
            purchase.gift_status = 'rejected'
            purchase.save()
            return Response({
                'message': 'Gift rejected.',
                'purchase': CoachingPackagePurchaseSerializer(purchase).data
            })
        else:
            return Response(
                {'error': 'Invalid action. Use "accept" or "reject".'},
                status=status.HTTP_400_BAD_REQUEST
            )


class SessionTransferViewSet(viewsets.ModelViewSet):
    serializer_class = SessionTransferSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        base_qs = SessionTransfer.objects.select_related(
            'from_user', 'to_user', 'package_purchase'
        )
        
        if user.role in ['admin', 'staff']:
            return base_qs
        
        # Users see transfers they sent or received
        return base_qs.filter(
            Q(from_user=user) | 
            Q(to_user_phone=user.phone) |
            Q(to_user=user)
        )
    
    def perform_create(self, serializer):
        serializer.save(from_user=self.request.user)
    
    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        """Claim a session transfer"""
        transfer = self.get_object()
        
        # Verify recipient
        if transfer.to_user_phone != request.user.phone:
            return Response(
                {'error': 'You are not authorized to claim this transfer.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        if transfer.transfer_status != 'pending':
            return Response(
                {'error': f'This transfer is already {transfer.transfer_status}.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check expiration
        if transfer.expires_at and transfer.expires_at < timezone.now():
            transfer.transfer_status = 'expired'
            transfer.save()
            return Response(
                {'error': 'This transfer has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        action = request.data.get('action', 'accept')
        
        if action == 'accept':
            # Create or update recipient's package purchase
            package_purchase = transfer.package_purchase
            
            # Check if recipient already has this package
            recipient_purchase = CoachingPackagePurchase.objects.filter(
                client=request.user,
                package=package_purchase.package,
                package_status='active'
            ).first()
            
            if recipient_purchase:
                # Add sessions to existing purchase
                recipient_purchase.sessions_remaining += transfer.session_count
                recipient_purchase.sessions_total += transfer.session_count
                recipient_purchase.save()
            else:
                # Create new purchase for recipient
                recipient_purchase = CoachingPackagePurchase.objects.create(
                    client=request.user,
                    package=package_purchase.package,
                    sessions_total=transfer.session_count,
                    sessions_remaining=transfer.session_count,
                    purchase_type='normal',
                    package_status='active'
                )
            
            # Deduct sessions from original purchase
            package_purchase.consume_session(transfer.session_count)
            
            # Update transfer
            transfer.transfer_status = 'accepted'
            transfer.to_user = request.user
            transfer.save()
            
            return Response({
                'message': 'Transfer accepted successfully.',
                'transfer': SessionTransferSerializer(transfer).data,
                'new_purchase': CoachingPackagePurchaseSerializer(recipient_purchase).data
            })
        
        elif action == 'reject':
            transfer.transfer_status = 'rejected'
            transfer.save()
            return Response({
                'message': 'Transfer rejected.',
                'transfer': SessionTransferSerializer(transfer).data
            })
        else:
            return Response(
                {'error': 'Invalid action. Use "accept" or "reject".'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending transfers for current user"""
        pending_transfers = SessionTransfer.objects.filter(
            to_user_phone=request.user.phone,
            transfer_status='pending',
            expires_at__gt=timezone.now()
        )
        serializer = self.get_serializer(pending_transfers, many=True)
        return Response(serializer.data)


class UserPhoneCheckView(APIView):
    """Check if user exists by phone number"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        phone = request.query_params.get('phone')
        if not phone:
            return Response(
                {'error': 'Phone parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            user = User.objects.get(phone=phone)
            return Response({
                'exists': True,
                'phone': user.phone,
                'name': user.get_full_name() or user.username,
                'username': user.username
            })
        except User.DoesNotExist:
            return Response({
                'exists': False,
                'phone': phone
            })