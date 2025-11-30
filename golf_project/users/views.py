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
        
        # Update OTP for existing user
        user.otp_code = otp
        user.otp_created_at = timezone.now()
        
        # Save default location ID to user
        resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
        if resolved_location:
            user.ghl_location_id = resolved_location
        
        user.save(update_fields=['otp_code', 'otp_created_at', 'ghl_location_id'] if resolved_location else ['otp_code', 'otp_created_at'])
        
        # Sync with GHL when OTP is requested (create/update contact with OTP code) - via Celery
        if resolved_location:
            try:
                if CELERY_AVAILABLE and sync_user_contact_task:
                    # Queue async task to sync with GHL
                    sync_user_contact_task.delay(
                        user.id,
                        location_id=resolved_location,
                        tags=None,  # REMOVED: tags
                        custom_fields={
                            'login_otp': otp,  # Store the OTP code in GHL
                        },
                    )
                    logger.info("Queued GHL sync task for user %s (OTP request)", user.id)
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
            
            # Check if OTP is valid and not expired (5 minutes)
            if (user.otp_code == otp and 
                user.otp_created_at and 
                timezone.now() - user.otp_created_at < timedelta(minutes=5)):
                
                user.otp_code = None
                user.otp_created_at = None
                user.phone_verified = True
                
                # Save default location ID to user
                resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                if resolved_location:
                    user.ghl_location_id = resolved_location
                
                update_fields = ['otp_code', 'otp_created_at', 'phone_verified']
                if resolved_location:
                    update_fields.append('ghl_location_id')
                user.save(update_fields=update_fields)
                
                logger.info("OTP verification for user %s (phone: %s)", user.id, user.phone)
                
                # Get or create authentication token
                token, created = Token.objects.get_or_create(user=user)
                
                logger.info("Resolved GHL location for sync: %s", resolved_location)
                if resolved_location:
                    try:
                        if CELERY_AVAILABLE and sync_user_contact_task:
                            # Queue async task to update last_login_at
                            sync_user_contact_task.delay(
                                user.id,
                                location_id=resolved_location,
                                tags=None,  # REMOVED: tags
                                custom_fields={
                                    'last_login_at': timezone.now().isoformat(),
                                },
                            )
                            logger.info("Queued GHL sync task for user %s (OTP verification)", user.id)
                        else:
                            # Fallback to synchronous call if Celery not available
                            from ghl.services import sync_user_contact
                            sync_user_contact(
                                user,
                                location_id=resolved_location,
                                tags=None,  # REMOVED: tags
                                custom_fields={
                                    'last_login_at': timezone.now().isoformat(),
                                },
                            )
                            logger.info("Successfully updated last_login_at for user %s in GHL location %s", user.phone, resolved_location)
                    except Exception as exc:
                        logger.warning("Failed to update GHL for OTP verification %s: %s", user.phone, exc)
                        # Don't fail the login if GHL sync fails
                
                return Response({
                    'token': token.key,
                    'user': UserSerializer(user).data,
                    'message': 'Login successful'
                })
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
        
        # Save default location ID to user
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
            # Use default location from settings
            resolved_location = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            
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
        
        # Create authentication token
        token, created = Token.objects.get_or_create(user=user)
        
        return Response({
            'message': 'User created successfully',
            'token': token.key,
            'user': UserSerializer(user).data,
            'converted_purchases_count': len(converted_purchases)
        }, status=status.HTTP_201_CREATED)
    
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

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def profile(request):
    """Get current user profile"""
    serializer = UserSerializer(request.user)
    return Response(serializer.data, status=status.HTTP_200_OK)

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