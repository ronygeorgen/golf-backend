from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.utils import timezone
from datetime import timedelta
import random
from .models import User
from .serializers import (
    PhoneLoginSerializer, 
    VerifyOTPSerializer, 
    UserSerializer,
    SignupSerializer,
    LoginSerializer
)

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
        
        # Create or get user
        user, created = User.objects.get_or_create(
            phone=phone,
            defaults={
                'username': phone,
                'otp_code': otp,
                'otp_created_at': timezone.now()
            }
        )
        
        if not created:
            user.otp_code = otp
            user.otp_created_at = timezone.now()
            user.save()
        
        # TODO: Integrate with GHL API to send OTP
        # This is where you'll call GHL API to update custom field and send SMS
        
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
        
        try:
            user = User.objects.get(phone=phone)
            
            # Check if OTP is valid and not expired (5 minutes)
            if (user.otp_code == otp and 
                user.otp_created_at and 
                timezone.now() - user.otp_created_at < timedelta(minutes=5)):
                
                user.otp_code = None
                user.otp_created_at = None
                user.phone_verified = True
                user.save()
                
                # Get or create authentication token
                token, created = Token.objects.get_or_create(user=user)
                
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
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
        user.save()
        
        # Print OTP to terminal for development/testing
        print("\n" + "="*50)
        print(f"🔐 OTP GENERATED FOR SIGNUP")
        print(f"👤 User: {user.email} ({user.username})")
        print(f"📱 Phone: {user.phone}")
        print(f"🔑 OTP Code: {otp}")
        print(f"⏰ Generated at: {timezone.now()}")
        print("="*50 + "\n")
        
        # Create authentication token
        token, created = Token.objects.get_or_create(user=user)
        
        return Response({
            'message': 'User created successfully',
            'token': token.key,
            'user': UserSerializer(user).data
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