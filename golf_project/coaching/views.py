import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ghl.services import (
    purchase_custom_fields,
)

try:
    from ghl.tasks import sync_purchase_with_ghl_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    sync_purchase_with_ghl_task = None
from users.models import User
from .models import (
    CoachingPackage, CoachingPackagePurchase, SessionTransfer, TempPurchase, PendingRecipient,
    SimulatorPackage, SimulatorPackagePurchase, SimulatorHoursTransfer
)
from .serializers import (
    CoachingPackageSerializer,
    CoachingPackagePurchaseSerializer,
    SessionTransferSerializer,
    TempPurchaseSerializer,
    PendingRecipientSerializer,
    SimulatorPackageSerializer,
    SimulatorPackagePurchaseSerializer,
    SimulatorHoursTransferSerializer,
)
from django.db import transaction

logger = logging.getLogger(__name__)

class TenPerPagePagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100
    
    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'total_pages': self.page.paginator.num_pages,
            'current_page': self.page.number,
            'page_size': self.page_size,
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data
        })

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
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        queryset = CoachingPackage.objects.all().order_by('-id')
        
        # Filter by location_id (skip for superadmins)
        is_privileged = self.request.user.is_authenticated and (getattr(self.request.user, 'role', None) == 'superadmin' or self.request.user.is_superuser)
        if location_id and not is_privileged:
            queryset = queryset.filter(
                Q(location_id=location_id) | 
                Q(location_id__isnull=True)
            )
        
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
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        package = serializer.save(location_id=location_id)
        
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
        # Get filtered queryset (includes location_id filtering from get_queryset)
        queryset = self.get_queryset()
        
        # Only return packages that have a redirect_url (for client-side) OR are TPI assessments
        active_packages = queryset.filter(is_active=True).filter(
            Q(is_tpi_assessment=True) |
            (Q(redirect_url__isnull=False) & ~Q(redirect_url=''))
        )
        
        serializer = self.get_serializer(active_packages, many=True)
        return Response(serializer.data)


class CoachingPackagePurchaseViewSet(viewsets.ModelViewSet):
    serializer_class = CoachingPackagePurchaseSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        from users.utils import get_location_id_from_request
        user = self.request.user
        location_id = get_location_id_from_request(self.request)
        base_qs = CoachingPackagePurchase.objects.select_related(
            'client', 'package', 'original_owner'
        ).prefetch_related('package__staff_members', 'organization_members')
        
        is_privileged = user.role in ['admin', 'staff', 'superadmin'] or user.is_superuser
        
        if is_privileged:
            # Filter by location_id for admin/staff
            if location_id and not user.is_superuser and user.role != 'superadmin':
                # Filter by package location_id (allow global packages)
                base_qs = base_qs.filter(
                    Q(package__location_id=location_id) | 
                    Q(package__location_id__isnull=True)
                )
            return base_qs
        
        # Clients see their own purchases, gifts received, and organization packages where they are members
        from coaching.models import OrganizationPackageMember
        # Get organization packages where user is a member (by phone or by user)
        org_purchase_ids = OrganizationPackageMember.objects.filter(
            Q(phone=user.phone) | Q(user=user)
        ).values_list('package_purchase_id', flat=True)
        
        return base_qs.filter(
            Q(client=user) | 
            Q(recipient_phone=user.phone, gift_status='accepted') |
            Q(id__in=org_purchase_ids)
        )
    
    def perform_create(self, serializer):
        package = serializer.validated_data.get('package')
        purchase_type = serializer.validated_data.get('purchase_type', 'normal')
        location_id = get_location_id_from_request(self.request)
        
        if not package or not package.is_active:
            raise serializers.ValidationError("Selected package is not available.")
        
        # Verify package belongs to location (if location_id is provided)
        if location_id and package.location_id != location_id:
            raise serializers.ValidationError("Selected package is not available for your location.")
        
        # For gift purchases, set client to recipient when creating
        if purchase_type == 'gift':
            recipient_phone = serializer.validated_data.get('recipient_phone')
            try:
                recipient = User.objects.get(phone=recipient_phone)
                purchase = serializer.save(client=recipient, original_owner=self.request.user)
            except User.DoesNotExist:
                raise serializers.ValidationError("Recipient not found.")
        elif purchase_type == 'organization':
            # For organization purchases, client is the purchaser
            purchase = serializer.save(client=self.request.user)
        else:
            purchase = serializer.save(client=self.request.user)

        self._sync_purchase_with_ghl(purchase)
        
        # Update GHL custom fields for the purchase owner
        try:
            from ghl.services import update_user_ghl_custom_fields
            location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            update_user_ghl_custom_fields(purchase.client, location_id=location_id)
            # Also update original owner if different (for gifts)
            if purchase.original_owner and purchase.original_owner != purchase.client:
                update_user_ghl_custom_fields(purchase.original_owner, location_id=location_id)
        except Exception as exc:
            logger.warning("Failed to update GHL custom fields after purchase %s: %s", purchase.id, exc)
    
    @action(detail=False, methods=['get'])
    def my(self, request):
        # Get personal/gifted purchases (exclude organization packages)
        # Only include purchases where user is the client or received as gift
        # The filter by client=request.user ensures staff don't see packages they referred to clients
        # (where they are the referrer but not the client)
        purchases = self.get_queryset().filter(
            Q(client=request.user) | 
            Q(recipient_phone=request.user.phone, gift_status='accepted')
        ).exclude(
            purchase_type='organization'
        ).order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def user_purchases(self, request):
        """Get purchases for a specific user (Admin/Staff/Superadmin only)"""
        if request.user.role not in ['admin', 'staff', 'superadmin']:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
        user_id = request.query_params.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        target_user = get_object_or_404(User, id=user_id)
        
        # Get personal/gifted purchases (exclude organization packages)
        # Only include purchases where user is the client or received as gift
        purchases = self.get_queryset().filter(
            Q(client=target_user) | 
            Q(recipient_phone=target_user.phone, gift_status='accepted')
        ).exclude(
            purchase_type='organization'
        ).order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
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
    
    @action(detail=False, methods=['get'])
    def organization_packages(self, request):
        """Get organization packages where user is a member"""
        from coaching.models import OrganizationPackageMember
        
        org_purchases = CoachingPackagePurchase.objects.filter(
            purchase_type='organization',
            package_status='active',
            sessions_remaining__gt=0,
            organization_members__phone=request.user.phone
        ).distinct().select_related('package', 'client').prefetch_related('organization_members')
        
        serializer = self.get_serializer(org_purchases, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def my_organization_purchases(self, request):
        """Get organization purchases where user is the purchaser (client)"""
        org_purchases = CoachingPackagePurchase.objects.filter(
            purchase_type='organization',
            client=request.user
        ).select_related('package', 'client').prefetch_related('organization_members').order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(org_purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(org_purchases, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def transferable_purchases(self, request):
        """Get purchases available for transfer (excludes organization packages)"""
        purchases = self.get_queryset().filter(
            client=request.user,
            sessions_remaining__gt=0,
            package_status='active',
            gift_status__isnull=True  # Exclude pending gifts
        ).exclude(purchase_type='organization').order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def usage_details(self, request, pk=None):
        """Get usage details for a package purchase - who used sessions and simulator hours"""
        from bookings.models import Booking
        
        purchase = self.get_object()
        
        # Verify user has permission to view this purchase
        if purchase.purchase_type != 'organization':
            return Response(
                {'error': 'This endpoint is only for organization purchases.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Only the purchaser can view usage details
        if purchase.client != request.user and request.user.role not in ['admin', 'staff']:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Get all bookings that used this package purchase
        bookings = Booking.objects.filter(
            package_purchase=purchase
        ).select_related('client', 'coach', 'simulator', 'coaching_package').order_by('-start_time')
        
        # Separate coaching and simulator bookings
        coaching_bookings = bookings.filter(booking_type='coaching')
        simulator_bookings = bookings.filter(booking_type='simulator')
        
        # Aggregate usage by user for coaching sessions
        coaching_usage = {}
        for booking in coaching_bookings:
            user_id = booking.client.id
            user_name = f"{booking.client.first_name} {booking.client.last_name}"
            user_email = booking.client.email
            
            if user_id not in coaching_usage:
                coaching_usage[user_id] = {
                    'user_id': user_id,
                    'user_name': user_name,
                    'user_email': user_email,
                    'sessions_used': 0,
                    'bookings': []
                }
            
            coaching_usage[user_id]['sessions_used'] += 1
            coaching_usage[user_id]['bookings'].append({
                'id': booking.id,
                'date': booking.start_time.isoformat(),
                'coach': f"{booking.coach.first_name} {booking.coach.last_name}" if booking.coach else None,
                'status': booking.status
            })
        
        # Aggregate usage by user for simulator hours
        simulator_usage = {}
        for booking in simulator_bookings:
            user_id = booking.client.id
            user_name = f"{booking.client.first_name} {booking.client.last_name}"
            user_email = booking.client.email
            hours_used = Decimal(str(booking.duration_minutes)) / Decimal('60')
            
            if user_id not in simulator_usage:
                simulator_usage[user_id] = {
                    'user_id': user_id,
                    'user_name': user_name,
                    'user_email': user_email,
                    'hours_used': Decimal('0'),
                    'bookings': []
                }
            
            simulator_usage[user_id]['hours_used'] += hours_used
            simulator_usage[user_id]['bookings'].append({
                'id': booking.id,
                'date': booking.start_time.isoformat(),
                'duration_minutes': booking.duration_minutes,
                'simulator': booking.simulator.name if booking.simulator else None,
                'status': booking.status
            })
        
        # Convert Decimal to string for JSON serialization
        for user_id in simulator_usage:
            simulator_usage[user_id]['hours_used'] = float(simulator_usage[user_id]['hours_used'])
        
        return Response({
            'purchase_id': purchase.id,
            'purchase_name': purchase.purchase_name,
            'package_title': purchase.package.title,
            'sessions_total': purchase.sessions_total,
            'sessions_remaining': purchase.sessions_remaining,
            'sessions_used': purchase.sessions_total - purchase.sessions_remaining,
            'simulator_hours_total': float(purchase.simulator_hours_total),
            'simulator_hours_remaining': float(purchase.simulator_hours_remaining),
            'simulator_hours_used': float(purchase.simulator_hours_total - purchase.simulator_hours_remaining),
            'coaching_usage': list(coaching_usage.values()),
            'simulator_usage': list(simulator_usage.values()),
        })
    
    @action(detail=True, methods=['post'])
    def add_member(self, request, pk=None):
        """Add a member to an organization purchase"""
        from .models import OrganizationPackageMember, PendingRecipient
        
        purchase = self.get_object()
        
        # Validate purchase type
        if purchase.purchase_type != 'organization':
            return Response(
                {'error': 'This endpoint is only for organization purchases.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate user is the purchaser
        if purchase.client != request.user:
            return Response(
                {'error': 'Only the purchaser can manage members.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Validate purchase is active
        if purchase.package_status != 'active':
            return Response(
                {'error': 'Cannot add members to inactive purchases.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = request.data.get('phone')
        if not phone:
            return Response(
                {'error': 'Phone number is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = phone.strip()
        
        # Validate phone format
        if len(phone) < 10 or len(phone) > 15:
            return Response(
                {'error': 'Invalid phone number format. Phone must be 10-15 digits.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if already a member
        if OrganizationPackageMember.objects.filter(
            package_purchase=purchase,
            phone=phone
        ).exists():
            return Response(
                {'error': 'This phone number is already a member of this organization purchase.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if trying to add purchaser (they're already added)
        if phone == purchase.client.phone:
            return Response(
                {'error': 'The purchaser is already a member.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Try to get user, create member accordingly
        try:
            member_user = User.objects.get(phone=phone)
            # User exists - create member with user reference
            member, created = OrganizationPackageMember.objects.get_or_create(
                package_purchase=purchase,
                phone=phone,
                defaults={'user': member_user}
            )
            if not created:
                # Update user field if member existed without user
                member.user = member_user
                member.save()
        except User.DoesNotExist:
            # User doesn't exist - create member without user and create PendingRecipient
            member, created = OrganizationPackageMember.objects.get_or_create(
                package_purchase=purchase,
                phone=phone
            )
            # Create PendingRecipient for signup conversion
            PendingRecipient.objects.get_or_create(
                package=purchase.package,
                buyer=purchase.client,
                recipient_phone=phone,
                purchase_type='organization',
                defaults={'status': 'pending'}
            )
        
        # Refresh purchase to get updated members
        purchase.refresh_from_db()
        
        # Update GHL custom fields for the new member if they have an account
        try:
            from ghl.services import update_user_ghl_custom_fields
            location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            if member_user:
                update_user_ghl_custom_fields(member_user, location_id=location_id)
        except Exception as exc:
            logger.warning("Failed to update GHL custom fields after adding member %s: %s", phone, exc)
        
        serializer = self.get_serializer(purchase)
        return Response({
            'message': 'Member added successfully.',
            'purchase': serializer.data
        }, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def remove_member(self, request, pk=None):
        """Remove a member from an organization purchase"""
        from .models import OrganizationPackageMember, PendingRecipient
        
        purchase = self.get_object()
        
        # Validate purchase type
        if purchase.purchase_type != 'organization':
            return Response(
                {'error': 'This endpoint is only for organization purchases.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate user is the purchaser
        if purchase.client != request.user:
            return Response(
                {'error': 'Only the purchaser can manage members.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Validate purchase is active
        if purchase.package_status != 'active':
            return Response(
                {'error': 'Cannot remove members from inactive purchases.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = request.data.get('phone')
        if not phone:
            return Response(
                {'error': 'Phone number is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = phone.strip()
        
        # Prevent removing purchaser
        if phone == purchase.client.phone:
            return Response(
                {'error': 'Cannot remove the purchaser from the organization purchase.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Try to remove member
        try:
            member = OrganizationPackageMember.objects.get(
                package_purchase=purchase,
                phone=phone
            )
            member.delete()
            
            # Also delete related PendingRecipient if exists
            PendingRecipient.objects.filter(
                package=purchase.package,
                buyer=purchase.client,
                recipient_phone=phone,
                purchase_type='organization',
                status='pending'
            ).delete()
            
            # Refresh purchase to get updated members
            purchase.refresh_from_db()
            serializer = self.get_serializer(purchase)
            return Response({
                'message': 'Member removed successfully.',
                'purchase': serializer.data
            }, status=status.HTTP_200_OK)
        except OrganizationPackageMember.DoesNotExist:
            return Response(
                {'error': 'Member not found in this organization purchase.'},
                status=status.HTTP_404_NOT_FOUND
            )

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

        location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
        if not location_id:
            return

        # Queue async task to sync purchase with GHL
        try:
            if CELERY_AVAILABLE and sync_purchase_with_ghl_task:
                sync_purchase_with_ghl_task.delay(purchase.id)
            else:
                # Fallback to synchronous call if Celery not available
                # Use the same pattern as async task
                from ghl.services import sync_user_contact, purchase_custom_fields
                amount = getattr(purchase.package, 'price', 0)
                purchase_name = purchase.purchase_name or (purchase.package.title if purchase.package else 'Unknown')
                custom_fields = purchase_custom_fields(purchase_name, amount)
                sync_user_contact(
                    contact_owner,
                    location_id=location_id,
                    tags=None,
                    custom_fields=custom_fields,
                )
        except Exception as exc:
            logger.warning("Failed to sync GHL for purchase %s: %s", purchase.id, exc)


class GiftClaimView(APIView):
    """Handle gift claim (accept/reject) for both coaching and simulator packages"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, token):
        # Try to find coaching package purchase first
        purchase = None
        simulator_purchase = None
        
        try:
            purchase = CoachingPackagePurchase.objects.get(
                gift_token=token,
                gift_status='pending'
            )
        except CoachingPackagePurchase.DoesNotExist:
            try:
                simulator_purchase = SimulatorPackagePurchase.objects.get(
                    gift_token=token,
                    gift_status='pending'
                )
            except SimulatorPackagePurchase.DoesNotExist:
                return Response(
                    {'error': 'Gift not found or already claimed.'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Use the appropriate purchase object
        active_purchase = purchase or simulator_purchase
        recipient_phone = active_purchase.recipient_phone
        
        # Verify recipient
        if recipient_phone != request.user.phone:
            return Response(
                {'error': 'You are not authorized to claim this gift.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Check expiration
        if active_purchase.gift_expires_at and active_purchase.gift_expires_at < timezone.now():
            active_purchase.gift_status = 'expired'
            active_purchase.save()
            return Response(
                {'error': 'This gift has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        action = request.data.get('action', 'accept')
        
        if action == 'accept':
            active_purchase.gift_status = 'accepted'
            active_purchase.package_status = 'active'
            active_purchase.client = request.user
            active_purchase.save()
            
            # Update GHL custom fields for both recipient and original owner
            try:
                from ghl.services import update_user_ghl_custom_fields
                location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                update_user_ghl_custom_fields(request.user, location_id=location_id)
                if active_purchase.original_owner:
                    update_user_ghl_custom_fields(active_purchase.original_owner, location_id=location_id)
            except Exception as exc:
                logger.warning("Failed to update GHL custom fields after gift acceptance %s: %s", active_purchase.id, exc)
            
            if purchase:
                serializer = CoachingPackagePurchaseSerializer(purchase)
            else:
                serializer = SimulatorPackagePurchaseSerializer(simulator_purchase)
            
            return Response({
                'message': 'Gift accepted successfully.',
                'purchase': serializer.data
            })
        elif action == 'reject':
            active_purchase.gift_status = 'rejected'
            active_purchase.save()
            
            if purchase:
                serializer = CoachingPackagePurchaseSerializer(purchase)
            else:
                serializer = SimulatorPackagePurchaseSerializer(simulator_purchase)
            
            return Response({
                'message': 'Gift rejected.',
                'purchase': serializer.data
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
        location_id = get_location_id_from_request(self.request)
        base_qs = SessionTransfer.objects.select_related(
            'from_user', 'to_user', 'package_purchase'
        )
        
        if user.role in ['admin', 'staff']:
            # Filter by location_id for admin/staff
            if location_id:
                # Filter by package location_id
                base_qs = base_qs.filter(package_purchase__package__location_id=location_id)
            return base_qs
        
        # Users see transfers they sent or received
        queryset = base_qs.filter(
            Q(from_user=user) | 
            Q(to_user_phone=user.phone) |
            Q(to_user=user)
        )
        # Also filter by location_id for clients
        if location_id:
            queryset = queryset.filter(package_purchase__package__location_id=location_id)
        return queryset
    
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
            
            # Update GHL custom fields for both sender and recipient
            try:
                from ghl.services import update_user_ghl_custom_fields
                location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                update_user_ghl_custom_fields(request.user, location_id=location_id)  # Recipient
                if transfer.from_user:
                    update_user_ghl_custom_fields(transfer.from_user, location_id=location_id)  # Sender
            except Exception as exc:
                logger.warning("Failed to update GHL custom fields after transfer acceptance %s: %s", transfer.id, exc)
            
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


class SimulatorHoursTransferViewSet(viewsets.ModelViewSet):
    serializer_class = SimulatorHoursTransferSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        location_id = get_location_id_from_request(self.request)
        base_qs = SimulatorHoursTransfer.objects.select_related(
            'from_user', 'to_user', 'package_purchase'
        )
        
        if user.role in ['admin', 'staff']:
            # Filter by location_id for admin/staff
            if location_id:
                # Filter by package location_id
                base_qs = base_qs.filter(package_purchase__package__location_id=location_id)
            return base_qs
        
        # Users see transfers they sent or received
        queryset = base_qs.filter(
            Q(from_user=user) | 
            Q(to_user_phone=user.phone) |
            Q(to_user=user)
        )
        # Also filter by location_id for clients
        if location_id:
            queryset = queryset.filter(package_purchase__package__location_id=location_id)
        return queryset
    
    def perform_create(self, serializer):
        serializer.save(from_user=self.request.user)
    
    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        """Claim a simulator hours transfer"""
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
            with transaction.atomic():
                # Check if package still has enough hours
                if transfer.package_purchase.hours_remaining < transfer.hours:
                    return Response(
                        {'error': 'Package no longer has enough hours for this transfer.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Consume hours from source package
                transfer.package_purchase.consume_hours(transfer.hours)
                
                # Create or update recipient's simulator credit
                from simulators.models import SimulatorCredit
                SimulatorCredit.objects.create(
                    client=request.user,
                    hours=transfer.hours,
                    status=SimulatorCredit.Status.AVAILABLE,
                    notes=f"Transferred from {transfer.from_user.get_full_name() or transfer.from_user.username}"
                )
                
                # Update transfer status
                transfer.to_user = request.user
                transfer.transfer_status = 'accepted'
                transfer.save()
            
            # Update GHL custom fields for both sender and recipient
            try:
                from ghl.services import update_user_ghl_custom_fields
                location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                update_user_ghl_custom_fields(request.user, location_id=location_id)  # Recipient
                if transfer.from_user:
                    update_user_ghl_custom_fields(transfer.from_user, location_id=location_id)  # Sender
            except Exception as exc:
                logger.warning("Failed to update GHL custom fields after simulator hours transfer acceptance %s: %s", transfer.id, exc)
            
            serializer = self.get_serializer(transfer)
            return Response({
                'message': 'Transfer accepted successfully.',
                'transfer': serializer.data
            }, status=status.HTTP_200_OK)
        
        elif action == 'reject':
            transfer.transfer_status = 'rejected'
            transfer.save()
            serializer = self.get_serializer(transfer)
            return Response({
                'message': 'Transfer rejected.',
                'transfer': serializer.data
            }, status=status.HTTP_200_OK)
        
        else:
            return Response(
                {'error': 'Invalid action. Use "accept" or "reject".'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending simulator hours transfers for current user"""
        pending_transfers = SimulatorHoursTransfer.objects.filter(
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


class CreateTempPurchaseView(APIView):
    """
    Create a temporary purchase record before redirecting to payment.
    Returns temp_id which is used in the redirect URL and webhook.
    """
    permission_classes = [AllowAny]  # Allow unauthenticated for flexibility
    
    @transaction.atomic
    def post(self, request):
        package_id = request.data.get('package_id')
        buyer_phone = request.data.get('buyer_phone')
        purchase_type = request.data.get('purchase_type', 'normal')
        recipients = request.data.get('recipients', [])
        referral_id = request.data.get('referral_id')  # Optional: staff user ID who referred this purchase
        
        # Validate required fields
        if not package_id:
            return Response(
                {'error': 'package_id is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not buyer_phone:
            return Response(
                {'error': 'buyer_phone is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate purchase_type
        valid_purchase_types = ['normal', 'gift', 'organization']
        if purchase_type not in valid_purchase_types:
            return Response(
                {'error': f'Invalid purchase_type. Must be one of: {", ".join(valid_purchase_types)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get package_type from request - REQUIRED to determine which table to check
        requested_package_type = request.data.get('package_type')  # 'coaching' or 'simulator'
        
        if not requested_package_type:
            return Response(
                {'error': 'package_type is required. Must be either "coaching" or "simulator".'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if requested_package_type not in ['coaching', 'simulator']:
            return Response(
                {'error': 'Invalid package_type. Must be either "coaching" or "simulator".'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # ALWAYS use the package_type specified by the frontend - check ONLY that table
        package = None
        simulator_package = None
        package_type = requested_package_type
        
        if requested_package_type == 'simulator':
            # Only check SimulatorPackage table
            try:
                simulator_package = SimulatorPackage.objects.get(id=package_id, is_active=True)
            except SimulatorPackage.DoesNotExist:
                return Response(
                    {'error': f'Simulator package with ID {package_id} not found or is inactive.'},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:  # requested_package_type == 'coaching'
            # Only check CoachingPackage table
            try:
                package = CoachingPackage.objects.get(id=package_id, is_active=True)
            except CoachingPackage.DoesNotExist:
                return Response(
                    {'error': f'Coaching package with ID {package_id} not found or is inactive.'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Get the appropriate package object for redirect_url check
        active_package = simulator_package if simulator_package else package
        
        # Validate package has redirect_url
        if not active_package.redirect_url or not active_package.redirect_url.strip():
            return Response(
                {'error': 'Package does not have a redirect URL configured.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate recipients based on purchase type
        if purchase_type == 'gift':
            if not recipients or len(recipients) != 1:
                return Response(
                    {'error': 'Gift purchases require exactly one recipient phone number.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        elif purchase_type == 'organization':
            if not recipients or len(recipients) == 0:
                return Response(
                    {'error': 'Organization purchases require at least one member phone number.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        elif purchase_type == 'normal':
            recipients = []  # Normal purchases don't have recipients
        
        # Validate referral_id if provided
        referral_user = None
        if referral_id:
            try:
                from users.models import User
                referral_user = User.objects.get(id=referral_id, role__in=['superadmin', 'admin', 'staff'])
            except User.DoesNotExist:
                return Response(
                    {'error': f'Referral user with ID {referral_id} not found or is not an administrative member.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Create temp purchase
        try:
            if package_type == 'simulator':
                temp_purchase = TempPurchase(
                    simulator_package=simulator_package,
                    buyer_phone=buyer_phone,
                    purchase_type=purchase_type,
                    package_type='simulator',  # Explicitly store package type
                    recipients=recipients if recipients else [],
                    referral_id=referral_user
                )
            else:
                temp_purchase = TempPurchase(
                    package=package,
                    buyer_phone=buyer_phone,
                    purchase_type=purchase_type,
                    package_type='coaching',  # Explicitly store package type
                    recipients=recipients if recipients else [],
                    referral_id=referral_user
                )
            
            # Ensure recipients is always a list (not None) for normal purchases
            if temp_purchase.recipients is None:
                temp_purchase.recipients = []
            
            # Validate before saving
            temp_purchase.full_clean()
            temp_purchase.save()
            
            logger.info(
                f"Temp purchase created successfully: temp_id={temp_purchase.temp_id}, buyer={buyer_phone}, "
                f"purchase_type={purchase_type}, package_type={temp_purchase.package_type}, "
                f"package_id={package.id if package else None}, simulator_package_id={simulator_package.id if simulator_package else None}, "
                f"created_at={temp_purchase.created_at}"
            )
            
            # Verify it was saved
            verify_purchase = TempPurchase.objects.get(temp_id=temp_purchase.temp_id)
            logger.info(f"Verified temp purchase exists in DB: temp_id={verify_purchase.temp_id}")
            
            redirect_url = simulator_package.redirect_url if simulator_package else package.redirect_url
            
            # Add referral_id to redirect URL if provided
            if referral_id:
                from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
                parsed = urlparse(redirect_url)
                query_params = parse_qs(parsed.query)
                query_params['referral_id'] = [str(referral_id)]
                new_query = urlencode(query_params, doseq=True)
                redirect_url = urlunparse((
                    parsed.scheme, parsed.netloc, parsed.path,
                    parsed.params, new_query, parsed.fragment
                ))
            
            return Response({
                'temp_id': str(temp_purchase.temp_id),
                'redirect_url': redirect_url,
                'message': 'Temporary purchase created successfully.'
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating temp purchase: {e}", exc_info=True)
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return Response(
                {'error': f'Failed to create temporary purchase: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PackagePurchaseWebhookView(APIView):
    """
    Webhook endpoint to create a package purchase after external payment verification.
    Receives recipient_phone (which contains temp_id), phone, package_id, and purchase_type.
    Retrieves TempPurchase, and creates actual purchases.
    Handles recipients who don't exist yet by creating PendingRecipient records.
    """
    permission_classes = [AllowAny]  # Webhook should be accessible without authentication
    
    @transaction.atomic
    def post(self, request):
        import uuid
        
        # New parameter name: recipient_phone contains the temp_id
        temp_id_str = request.data.get('recipient_phone')
        
        # Fallback to temp_id for backward compatibility
        if not temp_id_str:
            temp_id_str = request.data.get('temp_id')
        
        if not temp_id_str:
            return Response(
                {'error': 'recipient_phone (temp_id) is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Parse and validate temp_id
        try:
            temp_id = uuid.UUID(temp_id_str)
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid temp_id format in webhook: {temp_id_str}, error: {e}")
            return Response(
                {'error': 'Invalid recipient_phone (temp_id) format. Must be a valid UUID.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Log webhook attempt
        logger.info(f"Webhook called for temp_id: {temp_id_str}, phone: {request.data.get('phone')}")
        
        # Get temp purchase
        try:
            temp_purchase = TempPurchase.objects.get(temp_id=temp_id)
            logger.info(f"Temp purchase found: temp_id={temp_id}, buyer={temp_purchase.buyer_phone}, created_at={temp_purchase.created_at}, expired={temp_purchase.is_expired}")
        except TempPurchase.DoesNotExist:
            # Log additional debugging info
            recent_temp_purchases = TempPurchase.objects.order_by('-created_at')[:5]
            logger.error(
                f"Temp purchase not found: temp_id={temp_id_str}. "
                f"Recent temp purchases (last 5): {[(str(tp.temp_id), tp.buyer_phone, tp.created_at) for tp in recent_temp_purchases]}"
            )
            
            # Check if there are any temp purchases at all
            total_count = TempPurchase.objects.count()
            logger.error(f"Total temp purchases in database: {total_count}")
            
            # Try to find by string representation (case-insensitive)
            try:
                temp_purchase_by_str = TempPurchase.objects.filter(temp_id__iexact=temp_id_str).first()
                if temp_purchase_by_str:
                    logger.warning(f"Found temp purchase by case-insensitive search: {temp_purchase_by_str.temp_id}")
            except Exception as e:
                logger.error(f"Error searching by string: {e}")
            
            # Check if phone matches any recent temp purchases
            phone_from_request = request.data.get('phone')
            if phone_from_request:
                temp_by_phone = TempPurchase.objects.filter(buyer_phone=phone_from_request).order_by('-created_at').first()
                if temp_by_phone:
                    logger.warning(f"Found temp purchase for phone {phone_from_request}: temp_id={temp_by_phone.temp_id}, created_at={temp_by_phone.created_at}")
            
            return Response(
                {
                    'error': f'Temporary purchase with recipient_phone (temp_id) {temp_id_str} not found.',
                    'debug_info': {
                        'temp_id_received': temp_id_str,
                        'phone_received': phone_from_request,
                        'total_temp_purchases': total_count,
                        'recent_temp_purchases': [
                            {
                                'temp_id': str(tp.temp_id),
                                'buyer_phone': tp.buyer_phone,
                                'created_at': tp.created_at.isoformat() if tp.created_at else None,
                                'expired': tp.is_expired
                            }
                            for tp in recent_temp_purchases
                        ]
                    }
                },
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check if temp purchase is expired
        if temp_purchase.is_expired:
            return Response(
                {'error': 'Temporary purchase has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get buyer user
        try:
            buyer = User.objects.get(phone=temp_purchase.buyer_phone)
        except User.DoesNotExist:
            return Response(
                {'error': f'Buyer with phone number {temp_purchase.buyer_phone} not found.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Determine package type - use explicit package_type field (more reliable than inferring from foreign keys)
        # First, get both foreign keys to validate consistency
        package = temp_purchase.package
        simulator_package = temp_purchase.simulator_package
        
        # Get package_id from request (if provided) to help determine correct type when package_type is missing
        request_package_id = request.data.get('package_id')
        if request_package_id:
            try:
                request_package_id = int(request_package_id)
            except (ValueError, TypeError):
                request_package_id = None
        
        # Try to get explicit package_type field (may not exist if migration hasn't been run)
        try:
            package_type = temp_purchase.package_type
        except AttributeError:
            # Field doesn't exist yet (migration not run)
            package_type = None
        
        # PRIORITY 3 & 4: If package_type is not set, try to determine from request package_id and database
        if not package_type:
            # If we have request_package_id, check both tables to determine correct type
            if request_package_id:
                coaching_exists = CoachingPackage.objects.filter(id=request_package_id, is_active=True).exists()
                simulator_exists = SimulatorPackage.objects.filter(id=request_package_id, is_active=True).exists()
                
                if coaching_exists and simulator_exists:
                    # Both exist with same ID - this is the problematic case
                    # Check which foreign key in temp_purchase matches
                    if package and package.id == request_package_id:
                        package_type = 'coaching'
                        simulator_package = None  # Clear wrong one
                        logger.warning(
                            f"Both packages exist with ID={request_package_id}. "
                            f"TempPurchase has coaching package set. Using coaching."
                        )
                    elif simulator_package and simulator_package.id == request_package_id:
                        package_type = 'simulator'
                        package = None  # Clear wrong one
                        logger.warning(
                            f"Both packages exist with ID={request_package_id}. "
                            f"TempPurchase has simulator package set. Using simulator."
                        )
                    else:
                        # Neither matches or both are None - can't determine
                        logger.error(
                            f"Both packages exist with ID={request_package_id}, but temp_purchase has "
                            f"package_id={package.id if package else None}, "
                            f"simulator_package_id={simulator_package.id if simulator_package else None}. "
                            f"Cannot determine correct type!"
                        )
                        return Response(
                            {'error': f'Package ID {request_package_id} exists in both tables. Cannot determine package type from temp purchase.'},
                            status=status.HTTP_400_BAD_REQUEST
                        )
                elif coaching_exists:
                    package_type = 'coaching'
                    # Validate temp_purchase has coaching package
                    if simulator_package:
                        logger.warning(
                            f"Request package_id={request_package_id} is a coaching package, "
                            f"but temp_purchase has simulator_package_id={simulator_package.id}. "
                            f"Clearing simulator_package and using coaching."
                        )
                        simulator_package = None
                    if not package or package.id != request_package_id:
                        # Try to get the correct package
                        try:
                            package = CoachingPackage.objects.get(id=request_package_id, is_active=True)
                            logger.warning(f"Updated temp_purchase package to match request package_id={request_package_id}")
                        except CoachingPackage.DoesNotExist:
                            pass
                elif simulator_exists:
                    package_type = 'simulator'
                    # Validate temp_purchase has simulator package
                    if package:
                        logger.warning(
                            f"Request package_id={request_package_id} is a simulator package, "
                            f"but temp_purchase has package_id={package.id}. "
                            f"Clearing package and using simulator."
                        )
                        package = None
                    if not simulator_package or simulator_package.id != request_package_id:
                        # Try to get the correct package
                        try:
                            simulator_package = SimulatorPackage.objects.get(id=request_package_id, is_active=True)
                            logger.warning(f"Updated temp_purchase simulator_package to match request package_id={request_package_id}")
                        except SimulatorPackage.DoesNotExist:
                            pass
                else:
                    # Package doesn't exist in either table
                    logger.error(f"Package ID {request_package_id} not found in either CoachingPackage or SimulatorPackage")
                    return Response(
                        {'error': f'Package with ID {request_package_id} not found or is inactive.'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            else:
                # No request_package_id - infer from foreign keys (backward compatibility)
                if package and simulator_package:
                    logger.error(
                        f"TempPurchase {temp_purchase.temp_id} has BOTH package and simulator_package set! "
                        f"package_id={package.id}, simulator_package_id={simulator_package.id}. "
                        f"No package_id in request to determine correct type."
                    )
                    return Response(
                        {'error': 'Temp purchase has both package types set and no package_id in request to determine correct type.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif simulator_package:
                    package_type = 'simulator'
                else:
                    package_type = 'coaching'
            
            logger.warning(
                f"TempPurchase {temp_purchase.temp_id} missing package_type field (migration may not be run), "
                f"determined type: {package_type}. package_id={package.id if package else None}, "
                f"simulator_package_id={simulator_package.id if simulator_package else None}, "
                f"request_package_id={request_package_id}"
            )
        
        # Validate consistency and get the correct package object
        # If package_type came from request, we may need to fetch the package from database
        if package_type == 'simulator':
            # Ensure we have the simulator package
            if not simulator_package:
                # Try to get it from database if we have package_id
                if request_package_id:
                    try:
                        simulator_package = SimulatorPackage.objects.get(id=request_package_id, is_active=True)
                        logger.info(f"Fetched simulator_package from database: id={simulator_package.id}")
                    except SimulatorPackage.DoesNotExist:
                        logger.error(
                            f"package_type='simulator' but simulator_package not found in temp_purchase "
                            f"and package_id={request_package_id} not found in SimulatorPackage table!"
                        )
                        return Response(
                            {'error': f'Simulator package with ID {request_package_id} not found or is inactive.'},
                            status=status.HTTP_404_NOT_FOUND
                        )
                else:
                    logger.error(
                        f"TempPurchase {temp_purchase.temp_id} has package_type='simulator' but simulator_package is None "
                        f"and no package_id in request!"
                    )
                    return Response(
                        {'error': 'Data inconsistency: package_type is simulator but simulator_package is not set.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            # Clear package to ensure we use simulator_package
            package = None
        else:  # package_type == 'coaching'
            # Ensure we have the coaching package
            if not package:
                # Try to get it from database if we have package_id
                if request_package_id:
                    try:
                        package = CoachingPackage.objects.get(id=request_package_id, is_active=True)
                        logger.info(f"Fetched coaching package from database: id={package.id}")
                    except CoachingPackage.DoesNotExist:
                        logger.error(
                            f"package_type='coaching' but package not found in temp_purchase "
                            f"and package_id={request_package_id} not found in CoachingPackage table!"
                        )
                        return Response(
                            {'error': f'Coaching package with ID {request_package_id} not found or is inactive.'},
                            status=status.HTTP_404_NOT_FOUND
                        )
                else:
                    logger.error(
                        f"TempPurchase {temp_purchase.temp_id} has package_type='coaching' but package is None "
                        f"and no package_id in request!"
                    )
                    return Response(
                        {'error': 'Data inconsistency: package_type is coaching but package is not set.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
            # Clear simulator_package to ensure we use package
            simulator_package = None
        
        logger.info(
            f"Webhook processing temp_purchase {temp_purchase.temp_id}: package_type={package_type}, "
            f"package_id={package.id if package else None}, simulator_package_id={simulator_package.id if simulator_package else None}, "
            f"request_package_id={request_package_id}"
        )
        
        active_package = simulator_package if simulator_package else package
        
        purchase_type = temp_purchase.purchase_type
        recipients = temp_purchase.recipients or []
        
        # Handle normal purchase
        if purchase_type == 'normal':
            # Create the purchase (always create new purchase)
            try:
                if package_type == 'simulator':
                    # Create simulator package purchase
                    purchase = SimulatorPackagePurchase.objects.create(
                        client=buyer,
                        package=simulator_package,
                        purchase_type='normal',
                        purchase_name=simulator_package.title,
                        hours_total=simulator_package.hours,
                        hours_remaining=simulator_package.hours,
                        package_status='active',
                        gift_status=None,
                        expiry_date=(
                            (django_timezone.now().date() + timedelta(days=simulator_package.validity_days))
                            if simulator_package.validity_days else None
                        )
                    )
                    
                    logger.info(f"Simulator package purchase created via webhook: User {buyer.phone}, Package {simulator_package.id}, Purchase ID {purchase.id}")
                    
                    # Update GHL custom fields
                    try:
                        from ghl.services import update_user_ghl_custom_fields
                        location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                        update_user_ghl_custom_fields(buyer, location_id=location_id)
                    except Exception as exc:
                        logger.warning("Failed to update GHL custom fields after simulator package purchase %s: %s", purchase.id, exc)
                    
                    return Response({
                        'message': 'Simulator package purchase created successfully.',
                        'purchase_id': purchase.id,
                        'purchase': SimulatorPackagePurchaseSerializer(purchase).data
                    }, status=status.HTTP_201_CREATED)
                else:
                    # Create coaching package purchase
                    simulator_hours = Decimal(str(package.simulator_hours)) if package.simulator_hours else Decimal('0')
                    purchase = CoachingPackagePurchase.objects.create(
                        client=buyer,
                        package=package,
                        purchase_type='normal',
                        purchase_name=package.title,
                        sessions_total=package.session_count,
                        sessions_remaining=package.session_count,
                        simulator_hours_total=simulator_hours,
                        simulator_hours_remaining=simulator_hours,
                        package_status='active',
                        gift_status=None,
                        referral_id=temp_purchase.referral_id  # Copy referral_id from temp_purchase
                    )
                    
                    # Sync with GHL if available
                    if CELERY_AVAILABLE and sync_purchase_with_ghl_task:
                        try:
                            sync_purchase_with_ghl_task.delay(purchase.id)
                        except Exception as e:
                            logger.error(f"Failed to queue GHL sync for purchase {purchase.id}: {e}")
                    
                    logger.info(f"Package purchase created via webhook: User {buyer.phone}, Package {package.id}, Purchase ID {purchase.id}")
                    
                    # Update GHL custom fields
                    try:
                        from ghl.services import update_user_ghl_custom_fields
                        location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                        update_user_ghl_custom_fields(buyer, location_id=location_id)
                    except Exception as exc:
                        logger.warning("Failed to update GHL custom fields after coaching package purchase %s: %s", purchase.id, exc)
                    
                    return Response({
                        'message': 'Package purchase created successfully.',
                        'purchase_id': purchase.id,
                        'purchase': CoachingPackagePurchaseSerializer(purchase).data
                    }, status=status.HTTP_201_CREATED)
                
            except Exception as e:
                logger.error(f"Error creating package purchase via webhook: {e}")
                return Response(
                    {'error': f'Failed to create package purchase: {str(e)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        
        # Handle gift purchase
        elif purchase_type == 'gift':
            if not recipients or len(recipients) != 1:
                return Response(
                    {'error': 'Gift purchase requires exactly one recipient.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            recipient_phone = recipients[0]
            
            # Check if recipient exists
            try:
                recipient = User.objects.get(phone=recipient_phone)
                
                if buyer == recipient:
                    return Response(
                        {'error': 'You cannot gift a package to yourself.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Create gift purchase (always create new purchase)
                if package_type == 'simulator':
                    purchase = SimulatorPackagePurchase.objects.create(
                        client=recipient,
                        package=simulator_package,
                        purchase_type='gift',
                        purchase_name=simulator_package.title,
                        hours_total=simulator_package.hours,
                        hours_remaining=simulator_package.hours,
                        package_status='gifted',
                        gift_status='pending',
                        original_owner=buyer,
                        recipient_phone=recipient_phone,
                        expiry_date=(
                            (django_timezone.now().date() + timedelta(days=simulator_package.validity_days))
                            if simulator_package.validity_days else None
                        ),
                        gift_token=SimulatorPackagePurchase().generate_gift_token(),
                        gift_expires_at=timezone.now() + timedelta(days=30)
                    )
                else:
                    simulator_hours = Decimal(str(package.simulator_hours)) if package.simulator_hours else Decimal('0')
                    purchase = CoachingPackagePurchase.objects.create(
                        client=recipient,
                        package=package,
                        purchase_type='gift',
                        purchase_name=package.title,
                        sessions_total=package.session_count,
                        sessions_remaining=package.session_count,
                        simulator_hours_total=simulator_hours,
                        simulator_hours_remaining=simulator_hours,
                        package_status='gifted',
                        gift_status='pending',
                        original_owner=buyer,
                        recipient_phone=recipient_phone,
                        gift_token=CoachingPackagePurchase().generate_gift_token(),
                        gift_expires_at=timezone.now() + timedelta(days=30)
                    )
                
                package_id = simulator_package.id if package_type == 'simulator' else package.id
                logger.info(f"Gift purchase created via webhook: Buyer {buyer.phone}, Recipient {recipient_phone}, Package {package_id}, Purchase ID {purchase.id}")
                
                purchase_data = SimulatorPackagePurchaseSerializer(purchase).data if package_type == 'simulator' else CoachingPackagePurchaseSerializer(purchase).data
                
                return Response({
                    'message': 'Gift purchase created successfully.',
                    'purchase_id': purchase.id,
                    'purchase': purchase_data
                }, status=status.HTTP_201_CREATED)
                
            except User.DoesNotExist:
                # Recipient doesn't exist - create PendingRecipient
                # Note: PendingRecipient only supports CoachingPackage, not SimulatorPackage
                # For simulator packages, we'll need to handle this differently or skip pending recipient
                if package_type == 'simulator':
                    return Response(
                        {'error': 'Recipient must have an account to receive simulator package gifts.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                pending_recipient, created = PendingRecipient.objects.get_or_create(
                    package=package,
                    buyer=buyer,
                    recipient_phone=recipient_phone,
                    purchase_type='gift',
                    defaults={
                        'status': 'pending',
                        'temp_purchase': temp_purchase
                    }
                )
                
                if not created:
                    logger.warning(f"PendingRecipient already exists for {recipient_phone}")
                
                logger.info(f"PendingRecipient created for gift: Buyer {buyer.phone}, Recipient {recipient_phone}, Package {package.id}")
                
                return Response({
                    'message': 'Gift purchase pending. Recipient will receive package when they sign up.',
                    'pending_recipient_id': pending_recipient.id,
                    'recipient_phone': recipient_phone,
                    'status': 'pending_signup'
                }, status=status.HTTP_201_CREATED)
        
        # Handle organization purchase
        elif purchase_type == 'organization':
            # Simulator packages don't support organization purchases
            if package_type == 'simulator':
                return Response(
                    {'error': 'Simulator packages do not support organization purchases.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not recipients or len(recipients) == 0:
                return Response(
                    {'error': 'Organization purchase requires at least one member.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Filter out buyer's phone from recipients - only process other numbers
            other_recipients = [phone for phone in recipients if phone != buyer.phone]
            
            # Separate existing users and non-existing users (only from other recipients)
            existing_members = []
            pending_recipients_created = []
            
            for recipient_phone in other_recipients:
                try:
                    member_user = User.objects.get(phone=recipient_phone)
                    existing_members.append(member_user)
                except User.DoesNotExist:
                    # Non-existing users will be handled after purchase creation
                    pending_recipients_created.append(recipient_phone)
                    logger.info(f"Non-existing user for organization: Buyer {buyer.phone}, Member {recipient_phone}, Package {active_package.id}")
            
            # Create organization purchase (always create new purchase)
            try:
                from coaching.models import OrganizationPackageMember
                
                simulator_hours = Decimal(str(package.simulator_hours)) if package.simulator_hours else Decimal('0')
                purchase = CoachingPackagePurchase.objects.create(
                    client=buyer,
                    package=package,
                    purchase_type='organization',
                    purchase_name=package.title,
                    sessions_total=package.session_count,
                    sessions_remaining=package.session_count,
                    simulator_hours_total=simulator_hours,
                    simulator_hours_remaining=simulator_hours,
                    package_status='active',
                    gift_status=None
                )
                
                # Always add buyer as a member
                OrganizationPackageMember.objects.get_or_create(
                    package_purchase=purchase,
                    phone=buyer.phone,
                    defaults={'user': buyer}
                )
                
                # Add existing members (only other recipients, buyer already added)
                for member_user in existing_members:
                    OrganizationPackageMember.objects.get_or_create(
                        package_purchase=purchase,
                        phone=member_user.phone,
                        defaults={'user': member_user}
                    )
                
                # Add non-existing users as members (without user reference) and create PendingRecipient
                # This matches the behavior of the add_member endpoint
                for recipient_phone in pending_recipients_created:
                    # Create OrganizationPackageMember without user (user will be set when they sign up)
                    OrganizationPackageMember.objects.get_or_create(
                        package_purchase=purchase,
                        phone=recipient_phone
                    )
                    # Create PendingRecipient for signup conversion with direct link to purchase
                    PendingRecipient.objects.get_or_create(
                        package=package,
                        buyer=buyer,
                        recipient_phone=recipient_phone,
                        purchase_type='organization',
                        defaults={
                            'status': 'pending',
                            'temp_purchase': temp_purchase,
                            'package_purchase': purchase  # Direct link to the purchase
                        }
                    )
                    logger.info(f"OrganizationPackageMember and PendingRecipient created for organization: Buyer {buyer.phone}, Member {recipient_phone}, Package {package.id}")
                
                # Total members = buyer + existing members + non-existing members
                total_members = 1 + len(existing_members) + len(pending_recipients_created)
                
                logger.info(f"Organization purchase created via webhook: Buyer {buyer.phone}, Package {package.id}, Purchase ID {purchase.id}, Members: {total_members} (buyer + {len(existing_members)} others), Pending: {len(pending_recipients_created)}")
                
                return Response({
                    'message': 'Organization purchase created successfully.',
                    'purchase_id': purchase.id,
                    'purchase': CoachingPackagePurchaseSerializer(purchase).data,
                    'pending_recipients': pending_recipients_created,
                    'members_added': total_members
                }, status=status.HTTP_201_CREATED)
                
            except Exception as e:
                logger.error(f"Error creating organization purchase via webhook: {e}")
                return Response(
                    {'error': f'Failed to create organization purchase: {str(e)}'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        
        else:
            return Response(
                {'error': f'Invalid purchase_type: {purchase_type}'},
                status=status.HTTP_400_BAD_REQUEST
            )


class ListTempPurchasesView(APIView):
    """
    List all temporary purchases.
    Accessible to authenticated users (typically admins).
    """
    permission_classes = [AllowAny]
    
    def get(self, request):
        # Optional filters
        buyer_phone = request.query_params.get('buyer_phone')
        purchase_type = request.query_params.get('purchase_type')
        expired = request.query_params.get('expired')  # 'true' or 'false'
        
        queryset = TempPurchase.objects.all()
        
        # Apply filters
        if buyer_phone:
            queryset = queryset.filter(buyer_phone=buyer_phone)
        if purchase_type:
            queryset = queryset.filter(purchase_type=purchase_type)
        if expired == 'true':
            queryset = queryset.filter(expires_at__lt=timezone.now())
        elif expired == 'false':
            queryset = queryset.filter(expires_at__gte=timezone.now())
        
        # Order by most recent first
        queryset = queryset.order_by('-created_at')
        
        serializer = TempPurchaseSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'results': serializer.data
        }, status=status.HTTP_200_OK)


class ListPendingRecipientsView(APIView):
    """
    List all pending recipients.
    Accessible to authenticated users (typically admins).
    """
    permission_classes = [AllowAny]
    
    def get(self, request):
        # Optional filters
        buyer_id = request.query_params.get('buyer_id')
        recipient_phone = request.query_params.get('recipient_phone')
        purchase_type = request.query_params.get('purchase_type')
        status_filter = request.query_params.get('status')  # 'pending' or 'converted'
        package_id = request.query_params.get('package_id')
        
        queryset = PendingRecipient.objects.all()
        
        # Apply filters
        if buyer_id:
            queryset = queryset.filter(buyer_id=buyer_id)
        if recipient_phone:
            queryset = queryset.filter(recipient_phone=recipient_phone)
        if purchase_type:
            queryset = queryset.filter(purchase_type=purchase_type)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if package_id:
            queryset = queryset.filter(package_id=package_id)
        
        # Order by most recent first
        queryset = queryset.order_by('-created_at')
        
        serializer = PendingRecipientSerializer(queryset, many=True)
        return Response({
            'count': queryset.count(),
            'results': serializer.data
        }, status=status.HTTP_200_OK)


class SimulatorPackageViewSet(viewsets.ModelViewSet):
    queryset = SimulatorPackage.objects.all().order_by('-id')
    serializer_class = SimulatorPackageSerializer
    
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
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(self.request)
        queryset = SimulatorPackage.objects.all().order_by('-id')
        
        # Filter by location_id (skip for superadmins)
        is_privileged = self.request.user.is_authenticated and (getattr(self.request.user, 'role', None) == 'superadmin' or self.request.user.is_superuser)
        if location_id and not is_privileged:
            queryset = queryset.filter(
                Q(location_id=location_id) | 
                Q(location_id__isnull=True)
            )
        
        # Filter by active status if provided
        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')
        
        return queryset
    
    def perform_create(self, serializer):
        """Set location_id when creating simulator package and handle time restrictions"""
        from users.utils import get_location_id_from_request
        from coaching.models import SimulatorPackageTimeRestriction
        
        location_id = get_location_id_from_request(self.request)
        time_restrictions_data = self.request.data.get('time_restrictions', [])
        
        if location_id:
            package = serializer.save(location_id=location_id)
        else:
            package = serializer.save()
        
        # Handle time restrictions
        if time_restrictions_data:
            # Delete existing restrictions for this package
            SimulatorPackageTimeRestriction.objects.filter(package=package).delete()
            
            # Create new restrictions
            for restriction_data in time_restrictions_data:
                # Convert empty strings to None for date fields
                date_value = restriction_data.get('date')
                if date_value == '':
                    date_value = None
                
                day_of_week_value = restriction_data.get('day_of_week')
                if day_of_week_value == '':
                    day_of_week_value = None
                elif day_of_week_value is not None:
                    try:
                        day_of_week_value = int(day_of_week_value)
                    except (ValueError, TypeError):
                        day_of_week_value = None
                
                SimulatorPackageTimeRestriction.objects.create(
                    package=package,
                    is_recurring=restriction_data.get('is_recurring', True),
                    day_of_week=day_of_week_value,
                    date=date_value,
                    start_time=restriction_data.get('start_time'),
                    end_time=restriction_data.get('end_time'),
                    limit_hours=restriction_data.get('limit_hours', Decimal('1.0'))
                )
    
    def perform_update(self, serializer):
        """Handle time restrictions when updating simulator package"""
        from users.utils import get_location_id_from_request
        from coaching.models import SimulatorPackageTimeRestriction
        
        package = serializer.save()
        time_restrictions_data = self.request.data.get('time_restrictions', None)
        
        # Handle time restrictions if provided
        if time_restrictions_data is not None:
            # Delete existing restrictions for this package
            SimulatorPackageTimeRestriction.objects.filter(package=package).delete()
            
            # Create new restrictions
            for restriction_data in time_restrictions_data:
                # Convert empty strings to None for date fields
                date_value = restriction_data.get('date')
                if date_value == '':
                    date_value = None
                
                day_of_week_value = restriction_data.get('day_of_week')
                if day_of_week_value == '':
                    day_of_week_value = None
                elif day_of_week_value is not None:
                    try:
                        day_of_week_value = int(day_of_week_value)
                    except (ValueError, TypeError):
                        day_of_week_value = None
                
                SimulatorPackageTimeRestriction.objects.create(
                    package=package,
                    is_recurring=restriction_data.get('is_recurring', True),
                    day_of_week=day_of_week_value,
                    date=date_value,
                    start_time=restriction_data.get('start_time'),
                    end_time=restriction_data.get('end_time'),
                    limit_hours=restriction_data.get('limit_hours', Decimal('1.0'))
                )
    
    @action(detail=False, methods=['get'], url_path='active')
    def active_packages(self, request):
        """Get all active simulator packages"""
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(request)
        packages = SimulatorPackage.objects.filter(is_active=True)
        user = request.user
        is_privileged = user.is_authenticated and (getattr(user, 'role', None) == 'superadmin' or user.is_superuser)
        if location_id and not is_privileged:
            packages = packages.filter(
                Q(location_id=location_id) | 
                Q(location_id__isnull=True)
            )
        packages = packages.order_by('title')
        serializer = self.get_serializer(packages, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        package = self.get_object()
        package.is_active = not package.is_active
        package.save()
        return Response({
            'message': f'Package {"activated" if package.is_active else "deactivated"}',
            'is_active': package.is_active
        })


class SimulatorPackagePurchaseViewSet(viewsets.ModelViewSet):
    queryset = SimulatorPackagePurchase.objects.all().order_by('-purchased_at')
    serializer_class = SimulatorPackagePurchaseSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        location_id = get_location_id_from_request(self.request)
        queryset = SimulatorPackagePurchase.objects.all()
        
        is_privileged = user.role in ['admin', 'staff', 'superadmin'] or user.is_superuser
        
        # Regular users can only see their own purchases
        if not is_privileged:
            queryset = queryset.filter(client=user)
        elif location_id and not user.is_superuser and user.role != 'superadmin':
            # Filter by location_id for admin/staff (not superadmin)
            queryset = queryset.filter(
                Q(package__location_id=location_id) | 
                Q(package__location_id__isnull=True)
            )
        
        # Filter by purchase type
        purchase_type = self.request.query_params.get('purchase_type')
        if purchase_type:
            queryset = queryset.filter(purchase_type=purchase_type)
        
        # Filter by status
        package_status = self.request.query_params.get('package_status')
        if package_status:
            queryset = queryset.filter(package_status=package_status)
        
        return queryset.select_related('package', 'client', 'original_owner')
    
    @action(detail=False, methods=['get'], url_path='my')
    def my_purchases(self, request):
        """Get current user's simulator package purchases"""
        # Include purchases where user is the client, or where user received as accepted gift
        purchases = SimulatorPackagePurchase.objects.filter(
            Q(client=request.user) | 
            Q(recipient_phone=request.user.phone, gift_status='accepted')
        ).exclude(
            package_status='gifted'
        ).order_by('-purchased_at')
        
        # Debug: Log all purchases for this user
        logger.info(f"User {request.user.phone} - Total simulator purchases found: {purchases.count()}")
        for purchase in purchases:
            logger.info(
                f"  Purchase ID: {purchase.id}, Package: {purchase.package.title if purchase.package else 'N/A'}, "
                f"Status: {purchase.package_status}, Client: {purchase.client.phone}, "
                f"Gift Status: {purchase.gift_status}, Hours: {purchase.hours_remaining}/{purchase.hours_total}"
            )
        
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def transferable_purchases(self, request):
        """Get simulator package purchases available for transfer"""
        purchases = SimulatorPackagePurchase.objects.filter(
            client=request.user,
            hours_remaining__gt=0,
            package_status='active',
            gift_status__isnull=True  # Exclude pending gifts
        ).exclude(purchase_type='organization').order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)
    
    def perform_create(self, serializer):
        # Set client to current user if not admin
        if self.request.user.role not in ['admin', 'staff']:
            serializer.save(client=self.request.user)
        else:
            serializer.save()

    @action(detail=False, methods=['get'])
    def user_purchases(self, request):
        """Get purchases for a specific user (Admin/Staff/Superadmin only)"""
        if request.user.role not in ['admin', 'staff', 'superadmin']:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
            
        user_id = request.query_params.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        target_user = get_object_or_404(User, id=user_id)
        
        # Get personal/gifted purchases
        purchases = SimulatorPackagePurchase.objects.filter(
            Q(client=target_user) | 
            Q(recipient_phone=target_user.phone, gift_status='accepted')
        ).exclude(
            package_status='gifted'
        ).order_by('-purchased_at')
        
        # Apply pagination
        paginator = TenPerPagePagination()
        page = paginator.paginate_queryset(purchases, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(purchases, many=True)
        return Response(serializer.data)


class GuestPackagesView(APIView):
    """
    Get TPI assessment packages for a guest user by phone number.
    Returns packages and purchases with sessions remaining.
    """
    permission_classes = [AllowAny]
    
    def get_authenticators(self):
        """Override to disable authentication for guest packages"""
        return []
    
    def get(self, request):
        phone = request.query_params.get('phone')
        
        if not phone:
            return Response(
                {'error': 'Phone number is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = phone.strip()
        
        # Get user by phone (if exists)
        try:
            user = User.objects.get(phone=phone)
            location_id = user.ghl_location_id
        except User.DoesNotExist:
            # User doesn't exist yet, but might have purchases
            # Try to get location from a purchase
            from coaching.models import CoachingPackagePurchase
            purchase = CoachingPackagePurchase.objects.filter(
                client__phone=phone
            ).select_related('client').first()
            if purchase and purchase.client:
                location_id = purchase.client.ghl_location_id
            else:
                location_id = None
        
        # Get TPI assessment packages (active only)
        from coaching.models import CoachingPackage, CoachingPackagePurchase
        tpi_packages = CoachingPackage.objects.filter(
            is_tpi_assessment=True,
            is_active=True
        ).prefetch_related('staff_members')
        
        # Filter by location if available
        if location_id:
            tpi_packages = tpi_packages.filter(location_id=location_id)
        
        # Get purchases for this phone number with sessions remaining
        purchases = CoachingPackagePurchase.objects.filter(
            client__phone=phone,
            package__is_tpi_assessment=True,
            sessions_remaining__gt=0,
            package_status='active'
        ).select_related('package', 'client').order_by('-purchased_at')
        
        # Serialize packages and purchases
        from coaching.serializers import CoachingPackageSerializer, CoachingPackagePurchaseSerializer
        packages_data = CoachingPackageSerializer(tpi_packages, many=True).data
        purchases_data = CoachingPackagePurchaseSerializer(purchases, many=True).data
        
        return Response({
            'packages': packages_data,
            'purchases': purchases_data,
            'location_id': location_id
        }, status=status.HTTP_200_OK)