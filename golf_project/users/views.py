import logging
import random
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from .utils import get_location_id_from_request, filter_by_location, get_users_by_location

try:
    from ghl.tasks import sync_user_contact_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    sync_user_contact_task = None
from .models import User
from .serializers import (
    PhoneLoginSerializer, 
    VerifyOTPSerializer, 
    UserSerializer,
    SignupSerializer,
    LoginSerializer
)

logger = logging.getLogger(__name__)


class MemberListPagination(PageNumberPagination):
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
            'members': data
        })


@api_view(['GET'])
@permission_classes([AllowAny])
def list_ghl_locations(request):
    """
    List all active GHL locations for signup dropdown.
    Returns location_id and display name (company_name or location_id).
    GET /api/auth/ghl-locations/
    """
    try:
        from ghl.models import GHLLocation
        locations = GHLLocation.objects.filter(status='active').order_by('company_name', 'location_id')
        
        location_list = []
        for location in locations:
            display_name = location.company_name if location.company_name else location.location_id
            location_list.append({
                'location_id': location.location_id,
                'display_name': display_name,
                'company_name': location.company_name or '',
            })
        
        return Response({
            'locations': location_list,
            'count': len(location_list)
        }, status=status.HTTP_200_OK)
    except Exception as exc:
        logger.error("Failed to list GHL locations: %s", exc, exc_info=True)
        return Response({
            'error': 'Failed to fetch locations',
            'locations': [],
            'count': 0
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([AllowAny])
def request_otp(request):
    serializer = PhoneLoginSerializer(data=request.data)
    if serializer.is_valid():
        phone = serializer.validated_data['phone']
        
        # Generate 6-digit OTP
        otp = str(random.randint(100000, 999999))
        
        # Print OTP to terminal for development/testing
        print("\n" + "="*50)
        print(f"🔐 OTP GENERATED FOR LOGIN")
        print(f"📱 Phone: {phone}")
        print(f"🔑 OTP Code: {otp}")
        print(f"⏰ Generated at: {timezone.now()}")
        print("="*50 + "\n")
        
        # Get user - do not create if doesn't exist
        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            return Response({
                'error': 'User not found. Please sign up first.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Check if user account is paused
        if user.is_paused:
            return Response({
                'error': 'Your account has been paused. Please contact support.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Check if user account is active
        if not user.is_active:
            return Response({
                'error': 'User account is disabled.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Update OTP for existing user
        user.otp_code = otp
        user.otp_created_at = timezone.now()
        
        # Get user's ghl_location_id from database (priority) - this is what we use for GHL sync
        # Only update user's location if they don't have one and we get one from request
        resolved_location = user.ghl_location_id
        if not resolved_location:
            # If user doesn't have location_id, try to get from request or use default
            request_location_id = get_location_id_from_request(request)
            resolved_location = request_location_id or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            # Update user's ghl_location_id if we got one from request
            if request_location_id:
                user.ghl_location_id = request_location_id
        
        user.save(update_fields=['otp_code', 'otp_created_at', 'ghl_location_id'])
        
        # Sync with GHL when OTP is requested (create/update contact with OTP code) - via Celery
        # Always use user's ghl_location_id from database for sync
        if resolved_location:
            logger.info("GHL sync for OTP request - User ID: %s, User's ghl_location_id from DB: %s, Resolved location: %s", 
                       user.id, user.ghl_location_id, resolved_location)
            try:
                if CELERY_AVAILABLE and sync_user_contact_task:
                    # Queue async task to sync with GHL - use user's ghl_location_id from database
                    sync_user_contact_task.delay(
                        user.id,
                        location_id=resolved_location,  # This is user's ghl_location_id from DB
                        tags=None,  # REMOVED: tags
                        custom_fields={
                            'login_otp': otp,  # Store the OTP code in GHL
                        },
                    )
                    logger.info("Queued GHL sync task for user %s (OTP request) with location_id: %s", user.id, resolved_location)
                else:
                    # Fallback to synchronous call if Celery not available
                    from ghl.services import sync_user_contact
                    sync_user_contact(
                        user,
                        location_id=resolved_location,
                        tags=None,  # REMOVED: tags
                        custom_fields={
                            'login_otp': otp,
                        },
                    )
                    logger.info("Successfully synced user %s to GHL location %s during OTP request", user.phone, resolved_location)
            except Exception as exc:
                logger.warning("Failed to sync GHL for OTP request %s: %s", user.phone, exc)
                # Don't fail OTP request if GHL sync fails
        
        return Response({
            'message': 'OTP sent successfully',
            'phone': phone
        })
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def verify_otp(request):
    serializer = VerifyOTPSerializer(data=request.data)
    
    if serializer.is_valid():
        
        phone = serializer.validated_data['phone']
        otp = serializer.validated_data['otp']
        
        logger.info("Processing - Phone: %s, OTP: %s", phone, otp)

        print(f"🔍 DEBUG OTP VERIFICATION:")
        print(f"🔍 Phone: {phone}")
        print(f"🔍 OTP: {otp}")
        
        try:
            user = User.objects.get(phone=phone)
            
            # Check if user account is paused
            if user.is_paused:
                return Response({
                    'error': 'Your account has been paused. Please contact support.'
                }, status=status.HTTP_403_FORBIDDEN)
            
            # Check if user account is active
            if not user.is_active:
                return Response({
                    'error': 'User account is disabled.'
                }, status=status.HTTP_403_FORBIDDEN)
            
            # Check if OTP is valid and not expired (5 minutes)
            if (user.otp_code == otp and 
                user.otp_created_at and 
                timezone.now() - user.otp_created_at < timedelta(minutes=5)):
                
                # Capture the OTP before clearing it (needed for GHL sync)
                verified_otp = otp
                
                user.otp_code = None
                user.otp_created_at = None
                user.phone_verified = True
                
                # Get user's ghl_location_id from database (priority) - this is what we use for GHL sync
                # Only update user's location if they don't have one and we get one from request
                resolved_location = user.ghl_location_id
                if not resolved_location:
                    # If user doesn't have location_id, try to get from request or use default
                    request_location_id = get_location_id_from_request(request)
                    resolved_location = request_location_id or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                    # Update user's ghl_location_id if we got one from request
                    if request_location_id:
                        user.ghl_location_id = request_location_id
                
                user.save(update_fields=['otp_code', 'otp_created_at', 'phone_verified', 'ghl_location_id'])
                
                logger.info("OTP verification for user %s (phone: %s)", user.id, user.phone)
                
                # Get or create authentication token
                token, created = Token.objects.get_or_create(user=user)
                
                # Log location resolution for debugging
                logger.info("GHL sync for login - User ID: %s, User's ghl_location_id from DB: %s, Resolved location: %s", 
                           user.id, user.ghl_location_id, resolved_location)
                if resolved_location:
                    try:
                        # Store the OTP that was used for login in GHL custom field (like signup does)
                        # This ensures contact is created/updated in GHL during login (same as signup)
                        if CELERY_AVAILABLE and sync_user_contact_task:
                            # Queue async task to sync with GHL - create contact if doesn't exist, update if exists
                            # Store both OTP and last_login_at in custom fields (follows signup pattern)
                            sync_user_contact_task.delay(
                                user.id,
                                location_id=resolved_location,  # This is user's ghl_location_id from DB
                                tags=None,  # REMOVED: tags
                                custom_fields={
                                    'login_otp': verified_otp,  # Store the OTP code used for login (like signup)
                                    'last_login_at': timezone.now().isoformat(),
                                },
                            )
                            logger.info("Queued GHL sync task for user %s (OTP verification/login) with location_id: %s, OTP: %s", 
                                      user.id, resolved_location, verified_otp)
                        else:
                            # Fallback to synchronous call if Celery not available
                            from ghl.services import sync_user_contact
                            sync_user_contact(
                                user,
                                location_id=resolved_location,  # Use resolved location
                                tags=None,  # REMOVED: tags
                                custom_fields={
                                    'login_otp': verified_otp,  # Store the OTP code used for login (like signup)
                                    'last_login_at': timezone.now().isoformat(),
                                },
                            )
                            logger.info("Successfully synced user %s to GHL location %s during login (OTP: %s)", 
                                      user.phone, resolved_location, verified_otp)
                    except Exception as exc:
                        logger.warning("Failed to update GHL for OTP verification %s: %s", user.phone, exc)
                        # Don't fail the login if GHL sync fails
                
                response_data = {
                    'token': token.key,
                    'user': UserSerializer(user).data,
                    'message': 'Login successful',
                    'needs_dob': not bool(user.date_of_birth)  # True if DOB is missing
                }
                
                return Response(response_data)
            else:
                return Response({
                    'error': 'Invalid or expired OTP'
                }, status=status.HTTP_400_BAD_REQUEST)
                
        except User.DoesNotExist:
            return Response({
                'error': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)
    else:
        logger.error("❌ Serializer IS INVALID")
        logger.error("Serializer errors: %s", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    

def convert_pending_recipients(user):
    """
    Convert PendingRecipient records to actual purchases when user signs up.
    Called after user creation.
    """
    try:
        from coaching.models import PendingRecipient, CoachingPackagePurchase, OrganizationPackageMember
        from datetime import timedelta
        
        pending_recipients = PendingRecipient.objects.filter(
            recipient_phone=user.phone,
            status='pending'
        )
        
        if not pending_recipients.exists():
            return []
        
        converted_purchases = []
        
        for pending in pending_recipients:
            try:
                if pending.purchase_type == 'gift':
                    # Check if purchase already exists
                    existing_purchase = CoachingPackagePurchase.objects.filter(
                        client=user,
                        package=pending.package,
                        purchase_type='gift',
                        original_owner=pending.buyer,
                        recipient_phone=user.phone
                    ).first()
                    
                    if existing_purchase:
                        logger.warning(f"Gift purchase already exists for {user.phone}, skipping conversion")
                        pending.status = 'converted'
                        pending.save()
                        continue
                    
                    # Create gift purchase
                    purchase = CoachingPackagePurchase.objects.create(
                        client=user,
                        package=pending.package,
                        purchase_type='gift',
                        purchase_name=pending.package.title,
                        sessions_total=pending.package.session_count,
                        sessions_remaining=pending.package.session_count,
                        simulator_hours_total=pending.package.simulator_hours or 0,
                        simulator_hours_remaining=pending.package.simulator_hours or 0,
                        package_status='gifted',
                        gift_status='pending',
                        original_owner=pending.buyer,
                        recipient_phone=user.phone,
                        gift_token=CoachingPackagePurchase().generate_gift_token(),
                        gift_expires_at=timezone.now() + timedelta(days=30)
                    )
                    # Optionally link the purchase to PendingRecipient for reference
                    if not pending.package_purchase:
                        pending.package_purchase = purchase
                        pending.save()
                    converted_purchases.append(purchase)
                    logger.info(f"Converted pending gift to purchase: User {user.phone}, Purchase ID {purchase.id}")
                
                elif pending.purchase_type == 'organization':
                    # Use direct link to purchase if available (from webhook)
                    if pending.package_purchase:
                        org_purchase = pending.package_purchase
                        logger.info(f"Using direct purchase link: Purchase ID {org_purchase.id} for user {user.phone}")
                    else:
                        # Fallback: Find purchase (for backward compatibility with old records)
                        org_purchase = CoachingPackagePurchase.objects.filter(
                            client=pending.buyer,
                            package=pending.package,
                            purchase_type='organization'
                        ).first()
                        
                        if not org_purchase:
                            # Create organization purchase if it doesn't exist (shouldn't happen with new webhook)
                            org_purchase = CoachingPackagePurchase.objects.create(
                                client=pending.buyer,
                                package=pending.package,
                                purchase_type='organization',
                                purchase_name=pending.package.title,
                                sessions_total=pending.package.session_count,
                                sessions_remaining=pending.package.session_count,
                                package_status='active',
                                gift_status=None
                            )
                            
                            # Add buyer as member
                            OrganizationPackageMember.objects.get_or_create(
                                package_purchase=org_purchase,
                                phone=pending.buyer.phone,
                                defaults={'user': pending.buyer}
                            )
                            logger.info(f"Created organization purchase: Buyer {pending.buyer.phone}, Package {pending.package.id}, Purchase ID {org_purchase.id}")
                    
                    # Add this user as a member (or update if exists)
                    member, created = OrganizationPackageMember.objects.get_or_create(
                        package_purchase=org_purchase,
                        phone=user.phone,
                        defaults={'user': user}
                    )
                    # Update user field if member already existed but user was None
                    if not created:
                        if not member.user or member.user != user:
                            member.user = user
                            member.save()
                            logger.info(f"Updated member user field: Member ID {member.id}, User {user.phone}")
                    
                    converted_purchases.append(org_purchase)
                    logger.info(f"Added user to organization package: User {user.phone}, Purchase ID {org_purchase.id}, Member ID {member.id}, Created: {created}")
                
                # Mark pending recipient as converted
                pending.status = 'converted'
                pending.save()
                
            except Exception as e:
                logger.error(f"Error converting pending recipient {pending.id} for user {user.phone}: {e}")
                continue
        
        # Also check for OrganizationPackageMember records with user=None that match this user's phone
        from coaching.models import OrganizationPackageMember
        org_members_without_user = OrganizationPackageMember.objects.filter(
            phone=user.phone,
            user__isnull=True
        ).select_related('package_purchase', 'package_purchase__package')
        
        for member in org_members_without_user:
            try:
                # Update the member record to link the user
                member.user = user
                member.save()
                logger.info(f"Updated OrganizationPackageMember: Member ID {member.id}, User {user.phone}, Purchase ID {member.package_purchase.id}")
                
                # If purchase not already in converted_purchases, add it
                if member.package_purchase not in converted_purchases:
                    converted_purchases.append(member.package_purchase)
            except Exception as e:
                logger.error(f"Error updating OrganizationPackageMember {member.id} for user {user.phone}: {e}")
                continue
        
        return converted_purchases
        
    except Exception as e:
        logger.error(f"Error in convert_pending_recipients for user {user.phone}: {e}")
        return []


@api_view(['POST'])
@permission_classes([AllowAny])
def signup(request):
    """User registration endpoint"""
    serializer = SignupSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        
        # Generate OTP for phone verification during signup
        otp = str(random.randint(100000, 999999))
        user.otp_code = otp
        user.otp_created_at = timezone.now()
        
        # Save location ID to user (from request or default)
        location_id = request.data.get('ghl_location_id')
        if location_id:
            # Validate that the location exists and is active
            from ghl.models import GHLLocation
            try:
                location = GHLLocation.objects.get(location_id=location_id, status='active')
                user.ghl_location_id = location_id
            except GHLLocation.DoesNotExist:
                logger.warning("Invalid location_id %s provided during signup for user %s", location_id, user.id)
                # Fallback to default if provided location is invalid
                resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                if resolved_location:
                    user.ghl_location_id = resolved_location
        else:
            # Fallback to default location if not provided
            resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            if resolved_location:
                user.ghl_location_id = resolved_location
        
        user.save()
        
        # Print OTP to terminal for development/testing
        print("\n" + "="*50)
        print(f"🔐 OTP GENERATED FOR SIGNUP")
        print(f"👤 User: {user.email} ({user.username})")
        print(f"📱 Phone: {user.phone}")
        print(f"🔑 OTP Code: {otp}")
        print(f"⏰ Generated at: {timezone.now()}")
        print("="*50 + "\n")
        
        # Convert pending recipients to actual purchases
        converted_purchases = convert_pending_recipients(user)
        if converted_purchases:
            logger.info(f"Converted {len(converted_purchases)} pending recipients for new user {user.phone}")
        else:
            logger.info(f"No pending recipients found for new user {user.phone}")
        
        # Sync user to GHL (create contact if doesn't exist)
        try:
            # Use user's ghl_location_id if set, otherwise fallback to default
            resolved_location = user.ghl_location_id or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            
            if resolved_location:
                if CELERY_AVAILABLE and sync_user_contact_task:
                    # Queue async task to sync with GHL
                    sync_user_contact_task.delay(
                        user.id,
                        location_id=resolved_location,
                        tags=None,
                        custom_fields={
                            'login_otp': otp,  # Store the OTP code in GHL
                        },
                    )
                    logger.info("Queued GHL sync task for user %s (signup)", user.id)
                else:
                    # Fallback to synchronous call if Celery not available
                    from ghl.services import sync_user_contact
                    sync_user_contact(
                        user,
                        location_id=resolved_location,
                        tags=None,
                        custom_fields={
                            'login_otp': otp,
                        },
                    )
                    logger.info("Successfully synced user %s to GHL location %s during signup", user.phone, resolved_location)
            else:
                logger.warning("No GHL location available for user %s during signup", user.id)
        except Exception as exc:
            logger.warning("Failed to sync GHL for signup %s: %s", user.phone, exc)
            # Don't fail signup if GHL sync fails
        
        # Don't create token yet - user needs to verify OTP first
        # Token will be created in verify_otp endpoint after OTP verification
        
        return Response({
            'message': 'User created successfully. Please verify OTP to complete signup.',
            'phone': user.phone,
            'converted_purchases_count': len(converted_purchases)
        }, status=status.HTTP_201_CREATED)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def signup_without_otp(request):
    """User registration endpoint without OTP verification - for guest users or simulator bookings"""
    serializer = SignupSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        
        # Mark phone as verified (skip OTP verification)
        user.phone_verified = True
        
        # Save location ID to user (from request or default)
        location_id = request.data.get('ghl_location_id')
        if location_id:
            # Validate that the location exists and is active
            from ghl.models import GHLLocation
            try:
                location = GHLLocation.objects.get(location_id=location_id, status='active')
                user.ghl_location_id = location_id
            except GHLLocation.DoesNotExist:
                logger.warning("Invalid location_id %s provided during signup for user %s", location_id, user.id)
                # Fallback to default if provided location is invalid
                resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                if resolved_location:
                    user.ghl_location_id = resolved_location
        else:
            # Fallback to default location if not provided
            resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            if resolved_location:
                user.ghl_location_id = resolved_location
        
        user.save()
        
        # Convert pending recipients to actual purchases
        converted_purchases = convert_pending_recipients(user)
        if converted_purchases:
            logger.info(f"Converted {len(converted_purchases)} pending recipients for new user {user.phone}")
        else:
            logger.info(f"No pending recipients found for new user {user.phone}")
        
        # Sync user to GHL (create contact if doesn't exist)
        try:
            # Use user's ghl_location_id if set, otherwise fallback to default
            resolved_location = user.ghl_location_id or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            
            if resolved_location:
                if CELERY_AVAILABLE and sync_user_contact_task:
                    # Queue async task to sync with GHL
                    sync_user_contact_task.delay(
                        user.id,
                        location_id=resolved_location,
                        tags=None,
                        custom_fields=None,
                    )
                    logger.info("Queued GHL sync task for user %s (signup without OTP)", user.id)
                else:
                    # Fallback to synchronous call if Celery not available
                    from ghl.services import sync_user_contact
                    sync_user_contact(
                        user,
                        location_id=resolved_location,
                        tags=None,
                        custom_fields=None,
                    )
                    logger.info("Successfully synced user %s to GHL location %s during signup without OTP", user.phone, resolved_location)
            else:
                logger.warning("No GHL location available for user %s during signup without OTP", user.id)
        except Exception as exc:
            logger.warning("Failed to sync GHL for signup without OTP %s: %s", user.phone, exc)
            # Don't fail signup if GHL sync fails
        
        # Check if this is for simulator booking - if so, create token and log them in
        booking_type = request.data.get('booking_type')  # 'simulator' or 'coaching'
        response_data = {
            'message': 'User created successfully.',
            'converted_purchases_count': len(converted_purchases)
        }
        
        if booking_type == 'simulator':
            # For simulator bookings, create token and log them in
            token, created = Token.objects.get_or_create(user=user)
            response_data['token'] = token.key
            response_data['user'] = UserSerializer(user).data
            response_data['message'] = 'Registration successful. You can now book a simulator session.'
        else:
            # For coaching/TPI, user remains a guest (no token)
            response_data['message'] = 'User created successfully. Please login to continue.'
        
        return Response(response_data, status=status.HTTP_201_CREATED)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    """User login endpoint"""
    serializer = LoginSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.validated_data['user']
        # Get or create authentication token
        token, created = Token.objects.get_or_create(user=user)
        
        return Response({
            'message': 'Login successful',
            'token': token.key,
            'user': UserSerializer(user).data
        }, status=status.HTTP_200_OK)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """User logout endpoint - deletes the token"""
    try:
        request.user.auth_token.delete()
        return Response({
            'message': 'Logout successful'
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({
            'error': 'Error during logout'
        }, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def profile(request):
    """Get or update current user profile"""
    user = request.user
    
    if request.method == 'GET':
        serializer = UserSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    elif request.method == 'PUT':
        # Get current phone to check if it changed
        old_phone = user.phone
        new_phone = request.data.get('phone', old_phone)
        phone_changed = old_phone != new_phone
        
        # If phone changed, check if new phone already exists
        if phone_changed:
            if User.objects.filter(phone=new_phone).exclude(id=user.id).exists():
                return Response({
                    'error': 'This phone number is already registered to another account.'
                }, status=status.HTTP_400_BAD_REQUEST)
            # Reset phone verification when phone changes
            user.phone_verified = False
        
        # Check if DOB is being updated
        old_dob = user.date_of_birth
        new_dob = request.data.get('date_of_birth')
        dob_changed = False
        if new_dob:
            try:
                from datetime import datetime
                new_dob_date = datetime.strptime(new_dob, '%Y-%m-%d').date()
                dob_changed = old_dob != new_dob_date
            except (ValueError, TypeError):
                pass
        
        # Update user fields
        serializer = UserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            
            # Sync to GHL if any standard fields changed (including DOB)
            try:
                resolved_location = getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                if resolved_location:
                    if CELERY_AVAILABLE and sync_user_contact_task:
                        # Queue async task to sync with GHL
                        sync_user_contact_task.delay(
                            user.id,
                            location_id=None,  # Will use user's ghl_location_id
                            tags=None,
                            custom_fields=None,
                        )
                        logger.info("Queued GHL sync task for user %s (profile update)", user.id)
                    else:
                        # Fallback to synchronous call if Celery not available
                        from ghl.services import sync_user_contact
                        sync_user_contact(
                            user,
                            location_id=None,  # Will use user's ghl_location_id
                            tags=None,
                            custom_fields=None,
                        )
                        logger.info("Successfully synced profile for user %s to GHL", user.id)
            except Exception as exc:
                logger.warning("Failed to sync profile to GHL for user %s: %s", user.id, exc)
                # Don't fail the profile update if GHL sync fails
            
            response_data = {
                'message': 'Profile updated successfully',
                'user': serializer.data
            }
            
            # If phone changed, inform user they need to logout and login with new phone
            if phone_changed:
                response_data['phone_changed'] = True
                response_data['message'] = 'Profile updated. Please logout and login with your new phone number to verify it.'
            
            return Response(response_data, status=status.HTTP_200_OK)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def update_dob(request):
    """Update user's date of birth"""
    user = request.user
    date_of_birth = request.data.get('date_of_birth')
    
    if not date_of_birth:
        return Response({
            'error': 'date_of_birth is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        from datetime import datetime
        # Validate date format
        dob = datetime.strptime(date_of_birth, '%Y-%m-%d').date()
        
        # Validate date is not in the future
        from django.utils import timezone
        if dob > timezone.now().date():
            return Response({
                'error': 'Date of birth cannot be in the future'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user.date_of_birth = dob
        user.save(update_fields=['date_of_birth'])
        
        # Sync DOB to GHL
        try:
            resolved_location = getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            if resolved_location:
                if CELERY_AVAILABLE and sync_user_contact_task:
                    # Queue async task to sync with GHL
                    sync_user_contact_task.delay(
                        user.id,
                        location_id=None,  # Will use user's ghl_location_id
                        tags=None,
                        custom_fields=None,
                    )
                    logger.info("Queued GHL sync task for user %s (DOB update)", user.id)
                else:
                    # Fallback to synchronous call if Celery not available
                    from ghl.services import sync_user_contact
                    sync_user_contact(
                        user,
                        location_id=None,  # Will use user's ghl_location_id
                        tags=None,
                        custom_fields=None,
                    )
                    logger.info("Successfully synced DOB for user %s to GHL", user.id)
        except Exception as exc:
            logger.warning("Failed to sync DOB to GHL for user %s: %s", user.id, exc)
            # Don't fail the DOB update if GHL sync fails
        
        serializer = UserSerializer(user)
        return Response({
            'message': 'Date of birth updated successfully',
            'user': serializer.data
        }, status=status.HTTP_200_OK)
    except ValueError:
        return Response({
            'error': 'Invalid date format. Use YYYY-MM-DD'
        }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error("Error updating DOB for user %s: %s", user.id, e)
        return Response({
            'error': 'Failed to update date of birth'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([AllowAny])
def auto_login(request):
    """Auto-login endpoint for admin users via email query parameter"""
    email = request.query_params.get('email')
    
    if not email:
        return Response({
            'error': 'Email parameter is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        user = User.objects.get(email=email)
        
        # Check if user is admin (role='admin' or is_superuser=True)
        is_admin = user.role == 'admin' or user.is_superuser == True
        
        if not is_admin:
            return Response({
                'error': 'Auto-login is only available for admin users'
            }, status=status.HTTP_403_FORBIDDEN)
        
        if not user.is_active:
            return Response({
                'error': 'User account is disabled'
            }, status=status.HTTP_403_FORBIDDEN)
        
        if user.is_paused:
            return Response({
                'error': 'Your account has been paused. Please contact support.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Get or create authentication token
        token, created = Token.objects.get_or_create(user=user)
        
        return Response({
            'message': 'Auto-login successful',
            'token': token.key,
            'user': UserSerializer(user).data
        }, status=status.HTTP_200_OK)
        
    except User.DoesNotExist:
        return Response({
            'error': 'User not found with this email'
        }, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error in auto_login: {e}")
        return Response({
            'error': 'An error occurred during auto-login'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_list(request):
    """
    Get list of clients (members) for staff's location with custom fields.
    Only accessible by staff users.
    Returns clients with name, email, phone, and custom fields.
    """
    # Check if user is staff
    if request.user.role != 'staff':
        return Response({
            'error': 'Only staff members can access member list'
        }, status=status.HTTP_403_FORBIDDEN)
    
    # Get staff's location_id
    location_id = request.user.ghl_location_id
    if not location_id:
        return Response({
            'error': 'Staff member must have a location_id assigned'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        from ghl.services import (
            calculate_total_coaching_sessions,
            calculate_total_simulator_hours,
            get_last_active_package
        )
        from coaching.models import CoachingPackagePurchase, TempPurchase
        
        # Get search query parameter
        search_query = request.query_params.get('search', '').strip()
        
        # Get all clients for this location
        clients = User.objects.filter(
            role='client',
            ghl_location_id=location_id,
            is_active=True
        )
        
        # Apply search filter if provided
        if search_query:
            from django.db.models import Q
            # Split search query to handle full name searches (e.g., "John Doe")
            search_terms = search_query.split()
            
            if len(search_terms) > 1:
                # Multiple terms: try to match as full name (first + last)
                # Also search individual terms in all fields
                first_term = search_terms[0]
                last_term = ' '.join(search_terms[1:])
                
                q_objects = Q(
                    Q(first_name__icontains=first_term) & Q(last_name__icontains=last_term)
                ) | Q(
                    Q(first_name__icontains=last_term) & Q(last_name__icontains=first_term)
                )
                
                # Also search each term individually in all fields
                for term in search_terms:
                    q_objects |= (
                        Q(first_name__icontains=term) |
                        Q(last_name__icontains=term) |
                        Q(email__icontains=term) |
                        Q(phone__icontains=term)
                    )
                
                clients = clients.filter(q_objects)
            else:
                # Single term: search in all fields
                clients = clients.filter(
                    Q(first_name__icontains=search_query) |
                    Q(last_name__icontains=search_query) |
                    Q(email__icontains=search_query) |
                    Q(phone__icontains=search_query)
                )
        
        clients = clients.order_by('first_name', 'last_name', 'email')
        
        # Apply pagination only if no search query
        if search_query:
            # No pagination for search results
            page = clients
        else:
            paginator = MemberListPagination()
            page = paginator.paginate_queryset(clients, request)
            
            if page is None:
                # If pagination is not applied, return all (shouldn't happen with pagination)
                page = clients
        
        member_list_data = []
        staff_user_id = request.user.id
        
        for client in page:
            # Calculate custom fields
            total_sessions = calculate_total_coaching_sessions(client)
            total_hours = calculate_total_simulator_hours(client)
            last_package = get_last_active_package(client)
            
            # Get staff-referred purchases
            from coaching.models import CoachingPackagePurchase
            staff_referred_purchases = CoachingPackagePurchase.objects.filter(
                referral_id=staff_user_id,
                client=client,
                package_status='active'
            ).values('id', 'package__title', 'purchase_name', 'purchased_at')
            
            staff_referred_purchases = [
                {
                    'id': p['id'],
                    'package_name': p['package__title'],
                    'purchase_name': p['purchase_name'] or p['package__title'],
                    'purchased_at': p['purchased_at'].isoformat() if p['purchased_at'] else None
                }
                for p in staff_referred_purchases
            ]
            
            member_list_data.append({
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
            })
        
        # Return response (paginated if no search, otherwise all results)
        if search_query:
            return Response({
                'count': len(member_list_data),
                'total_pages': 1,
                'current_page': 1,
                'page_size': len(member_list_data),
                'members': member_list_data
            })
        else:
            # Return paginated response
            return paginator.get_paginated_response(member_list_data)
        
    except Exception as e:
        logger.error(f"Error in member_list: {e}", exc_info=True)
        return Response({
            'error': 'Failed to fetch member list'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)