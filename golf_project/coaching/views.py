import logging
from datetime import timedelta

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
    purchase_custom_fields,
)

try:
    from ghl.tasks import sync_purchase_with_ghl_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    sync_purchase_with_ghl_task = None
from users.models import User
from .models import CoachingPackage, CoachingPackagePurchase, SessionTransfer, TempPurchase, PendingRecipient
from .serializers import (
    CoachingPackageSerializer,
    CoachingPackagePurchaseSerializer,
    SessionTransferSerializer,
    TempPurchaseSerializer,
    PendingRecipientSerializer,
)
from django.db import transaction

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
        # Only return packages that have a redirect_url (for client-side)
        active_packages = CoachingPackage.objects.filter(
            is_active=True,
            redirect_url__isnull=False
        ).exclude(redirect_url='')
        serializer = self.get_serializer(active_packages, many=True)
        return Response(serializer.data)


class CoachingPackagePurchaseViewSet(viewsets.ModelViewSet):
    serializer_class = CoachingPackagePurchaseSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        base_qs = CoachingPackagePurchase.objects.select_related(
            'client', 'package', 'original_owner'
        ).prefetch_related('package__staff_members', 'organization_members')
        
        if user.role in ['admin', 'staff']:
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
        elif purchase_type == 'organization':
            # For organization purchases, client is the purchaser
            purchase = serializer.save(client=self.request.user)
        else:
            purchase = serializer.save(client=self.request.user)

        if location_id and self.request.user.ghl_location_id != location_id:
            self.request.user.ghl_location_id = location_id
            self.request.user.save(update_fields=['ghl_location_id'])

        self._sync_purchase_with_ghl(purchase)
    
    @action(detail=False, methods=['get'])
    def my(self, request):
        # Get personal/gifted purchases (exclude organization packages)
        purchases = self.get_queryset().filter(
            Q(client=request.user) | 
            Q(recipient_phone=request.user.phone, gift_status='accepted')
        ).exclude(purchase_type='organization')
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
        
        # Validate package exists and is active
        try:
            package = CoachingPackage.objects.get(id=package_id, is_active=True)
        except CoachingPackage.DoesNotExist:
            return Response(
                {'error': f'Package with ID {package_id} not found or is inactive.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Validate package has redirect_url
        if not package.redirect_url or not package.redirect_url.strip():
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
        
        # Create temp purchase
        try:
            temp_purchase = TempPurchase.objects.create(
                package=package,
                buyer_phone=buyer_phone,
                purchase_type=purchase_type,
                recipients=recipients
            )
            
            logger.info(f"Temp purchase created: temp_id={temp_purchase.temp_id}, buyer={buyer_phone}, type={purchase_type}")
            
            return Response({
                'temp_id': str(temp_purchase.temp_id),
                'redirect_url': package.redirect_url,
                'message': 'Temporary purchase created successfully.'
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating temp purchase: {e}")
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
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid recipient_phone (temp_id) format. Must be a valid UUID.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get temp purchase
        try:
            temp_purchase = TempPurchase.objects.get(temp_id=temp_id)
        except TempPurchase.DoesNotExist:
            return Response(
                {'error': f'Temporary purchase with recipient_phone (temp_id) {temp_id_str} not found.'},
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
        
        package = temp_purchase.package
        purchase_type = temp_purchase.purchase_type
        recipients = temp_purchase.recipients or []
        
        # Handle normal purchase
        if purchase_type == 'normal':
            # Check if purchase already exists
            existing_purchase = CoachingPackagePurchase.objects.filter(
                client=buyer,
                package=package,
                purchase_type='normal'
            ).first()
            
            if existing_purchase:
                logger.warning(f"Purchase already exists for user {buyer.phone} and package {package.id}")
                return Response({
                    'message': 'Purchase already exists.',
                    'purchase_id': existing_purchase.id,
                    'purchase': CoachingPackagePurchaseSerializer(existing_purchase).data
                }, status=status.HTTP_200_OK)
            
            # Create the purchase
            try:
                purchase = CoachingPackagePurchase.objects.create(
                    client=buyer,
                    package=package,
                    purchase_type='normal',
                    purchase_name=package.title,
                    sessions_total=package.session_count,
                    sessions_remaining=package.session_count,
                    package_status='active',
                    gift_status=None
                )
                
                # Sync with GHL if available
                if CELERY_AVAILABLE and sync_purchase_with_ghl_task:
                    try:
                        sync_purchase_with_ghl_task.delay(purchase.id)
                    except Exception as e:
                        logger.error(f"Failed to queue GHL sync for purchase {purchase.id}: {e}")
                
                logger.info(f"Package purchase created via webhook: User {buyer.phone}, Package {package.id}, Purchase ID {purchase.id}")
                
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
                
                # Check if purchase already exists
                existing_purchase = CoachingPackagePurchase.objects.filter(
                    client=recipient,
                    package=package,
                    purchase_type='gift',
                    original_owner=buyer,
                    recipient_phone=recipient_phone
                ).first()
                
                if existing_purchase:
                    logger.warning(f"Gift purchase already exists for recipient {recipient_phone} and package {package.id}")
                    return Response({
                        'message': 'Gift purchase already exists.',
                        'purchase_id': existing_purchase.id,
                        'purchase': CoachingPackagePurchaseSerializer(existing_purchase).data
                    }, status=status.HTTP_200_OK)
                
                # Create gift purchase
                purchase = CoachingPackagePurchase.objects.create(
                    client=recipient,
                    package=package,
                    purchase_type='gift',
                    purchase_name=package.title,
                    sessions_total=package.session_count,
                    sessions_remaining=package.session_count,
                    package_status='gifted',
                    gift_status='pending',
                    original_owner=buyer,
                    recipient_phone=recipient_phone,
                    gift_token=CoachingPackagePurchase().generate_gift_token(),
                    gift_expires_at=timezone.now() + timedelta(days=30)
                )
                
                logger.info(f"Gift purchase created via webhook: Buyer {buyer.phone}, Recipient {recipient_phone}, Package {package.id}, Purchase ID {purchase.id}")
                
                return Response({
                    'message': 'Gift purchase created successfully.',
                    'purchase_id': purchase.id,
                    'purchase': CoachingPackagePurchaseSerializer(purchase).data
                }, status=status.HTTP_201_CREATED)
                
            except User.DoesNotExist:
                # Recipient doesn't exist - create PendingRecipient
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
            if not recipients or len(recipients) == 0:
                return Response(
                    {'error': 'Organization purchase requires at least one member.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Separate existing users and non-existing users
            existing_members = []
            pending_recipients_created = []
            
            for recipient_phone in recipients:
                try:
                    member_user = User.objects.get(phone=recipient_phone)
                    existing_members.append(member_user)
                except User.DoesNotExist:
                    # Create PendingRecipient for non-existing users
                    pending_recipient, created = PendingRecipient.objects.get_or_create(
                        package=package,
                        buyer=buyer,
                        recipient_phone=recipient_phone,
                        purchase_type='organization',
                        defaults={
                            'status': 'pending',
                            'temp_purchase': temp_purchase
                        }
                    )
                    if created:
                        pending_recipients_created.append(recipient_phone)
                    logger.info(f"PendingRecipient created for organization: Buyer {buyer.phone}, Member {recipient_phone}, Package {package.id}")
            
            # If no existing members, return pending status
            if not existing_members:
                return Response({
                    'message': 'Organization purchase pending. All members will receive package when they sign up.',
                    'pending_recipients': pending_recipients_created,
                    'status': 'pending_signup'
                }, status=status.HTTP_201_CREATED)
            
            # Check if purchase already exists
            existing_purchase = CoachingPackagePurchase.objects.filter(
                client=buyer,
                package=package,
                purchase_type='organization'
            ).first()
            
            if existing_purchase:
                # Add existing members to the organization package
                from coaching.models import OrganizationPackageMember
                for member_user in existing_members:
                    OrganizationPackageMember.objects.get_or_create(
                        package_purchase=existing_purchase,
                        phone=member_user.phone,
                        defaults={'user': member_user}
                    )
                
                logger.warning(f"Organization purchase already exists, added members: User {buyer.phone}, Package {package.id}")
                return Response({
                    'message': 'Organization purchase already exists. Members added.',
                    'purchase_id': existing_purchase.id,
                    'purchase': CoachingPackagePurchaseSerializer(existing_purchase).data,
                    'pending_recipients': pending_recipients_created
                }, status=status.HTTP_200_OK)
            
            # Create organization purchase with existing members
            try:
                from coaching.models import OrganizationPackageMember
                
                purchase = CoachingPackagePurchase.objects.create(
                    client=buyer,
                    package=package,
                    purchase_type='organization',
                    purchase_name=package.title,
                    sessions_total=package.session_count,
                    sessions_remaining=package.session_count,
                    package_status='active',
                    gift_status=None
                )
                
                # Add purchaser as a member
                OrganizationPackageMember.objects.get_or_create(
                    package_purchase=purchase,
                    phone=buyer.phone,
                    defaults={'user': buyer}
                )
                
                # Add existing members
                for member_user in existing_members:
                    if member_user.phone != buyer.phone:  # Don't duplicate purchaser
                        OrganizationPackageMember.objects.get_or_create(
                            package_purchase=purchase,
                            phone=member_user.phone,
                            defaults={'user': member_user}
                        )
                
                logger.info(f"Organization purchase created via webhook: Buyer {buyer.phone}, Package {package.id}, Purchase ID {purchase.id}, Members: {len(existing_members)}, Pending: {len(pending_recipients_created)}")
                
                return Response({
                    'message': 'Organization purchase created successfully.',
                    'purchase_id': purchase.id,
                    'purchase': CoachingPackagePurchaseSerializer(purchase).data,
                    'pending_recipients': pending_recipients_created,
                    'members_added': len(existing_members)
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