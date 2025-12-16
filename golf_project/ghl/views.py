import logging
from urllib.parse import urlencode
from datetime import timedelta

try:
    import requests
except ImportError:
    requests = None

from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied

from .models import GHLLocation
from .serializers import GHLLocationSerializer, GHLOnboardSerializer
from .services import GHLClient, debug_contact_custom_fields, set_contact_custom_values

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([AllowAny])
def list_onboarded_locations(request):
    """
    List all onboarded GHL locations.
    Useful for debugging to see which location_ids are available.
    GET /api/ghlpage/locations/
    """
    locations = GHLLocation.objects.filter(status='active').order_by('-onboarded_at')
    serializer = GHLLocationSerializer(locations, many=True)
    return Response({
        'locations': serializer.data,
        'count': locations.count(),
    }, status=status.HTTP_200_OK)


class GHLOAuthAuthorizeView(APIView):
    """
    Initiate OAuth flow by redirecting to GHL authorization page.
    GET /api/ghlpage/oauth/authorize/?location_id=<id>
    """
    permission_classes = [AllowAny]

    def get(self, request):
        location_id = request.query_params.get('location_id')
        if not location_id:
            return Response(
                {"error": "location_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Store location_id in session or create location record
        location, _ = GHLLocation.objects.get_or_create(location_id=location_id)
        
        # Build OAuth authorization URL
        client_id = getattr(settings, 'GHL_CLIENT_ID', '')
        redirect_uri = getattr(settings, 'GHL_REDIRECTED_URI', '')
        scope = getattr(settings, 'GHL_SCOPE', '')
        auth_url = getattr(settings, 'GHL_AUTH_URL', '')
        
        if not all([client_id, redirect_uri, scope, auth_url]):
            return Response(
                {"error": "GHL OAuth configuration incomplete"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'scope': scope,
            'response_type': 'code',
        }
        
        auth_redirect_url = f"{auth_url}?{urlencode(params)}"
        return redirect(auth_redirect_url)


class GHLOAuthCallbackView(APIView):
    """
    Handle OAuth callback from GHL.
    Exchanges authorization code for tokens and saves them.
    GET /api/ghlpage/oauth/callback/?code=<code>&locationId=<location_id>
    """
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get('code')
        # locationId might come from query params OR from token response
        location_id = request.query_params.get('locationId')
        print(f"🔍 DEBUG: OAuth callback received")
        print(f"🔍 DEBUG: code = {code}")
        print(f"🔍 DEBUG: locationId from query = {location_id}")
        
        if not code:
            return Response(
                {"error": "Authorization code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Exchange code for tokens first (locationId comes in the response)
        client_id = getattr(settings, 'GHL_CLIENT_ID', '')
        client_secret = getattr(settings, 'GHL_CLIENT_SECRET', '')
        redirect_uri = getattr(settings, 'GHL_REDIRECTED_URI', '')
        base_url = getattr(settings, 'GHL_BASE_URL', 'https://services.leadconnectorhq.com')
        token_url = f"{base_url}/oauth/token"
        
        if not all([client_id, client_secret, redirect_uri]):
            return Response(
                {"error": "GHL OAuth configuration incomplete"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        payload = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'code': code,
        }
        
        try:
            if not requests:
                raise ImportError("requests library is required for OAuth token exchange")
            response = requests.post(token_url, data=payload, timeout=30)
            response.raise_for_status()
            token_data = response.json()
            print(f"🔍 Token data locationId: {token_data.get('locationId')}")
            print(f"🔍 Full token data keys: {token_data.keys()}")
        except requests.exceptions.HTTPError as exc:
            error_text = response.text if hasattr(response, 'text') else str(exc)
            logger.error("Failed to exchange OAuth code for tokens: %s - %s", exc, error_text, exc_info=True)
            return Response(
                {"error": "Failed to complete OAuth flow", "details": error_text},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as exc:
            logger.error("Failed to exchange OAuth code for tokens: %s", exc, exc_info=True)
            return Response(
                {"error": "Failed to complete OAuth flow"},
                status=status.HTTP_502_BAD_GATEWAY
            )
        
        # Get locationId from token response if not in query params
        if not location_id:
            location_id = token_data.get('locationId')
        
        if not location_id:
            return Response(
                {"error": "locationId not found in token response"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Decode the access token to see what location it's actually for
        access_token = token_data.get('access_token')
        if access_token:
            try:
                import base64
                import json as json_lib
                token_parts = access_token.split('.')
                if len(token_parts) >= 2:
                    payload = token_parts[1]
                    payload += '=' * (4 - len(payload) % 4)
                    decoded = base64.urlsafe_b64decode(payload)
                    token_payload = json_lib.loads(decoded)
                    actual_location = token_payload.get('authClassId') or token_payload.get('primaryAuthClassId')
                    print(f"🔍 ACTUAL LOCATION IN TOKEN: {actual_location}")
                    print(f"🔍 LOCATION WE'RE SAVING TO: {location_id}")
            except Exception as e:
                print(f"🔍 Error decoding token in callback: {e}")
        
        # Get location name from GHL API (optional)
        location_name = None
        try:
            access_token = token_data.get('access_token')
            if access_token:
                # Try to get location name
                location_info_url = f"{base_url}/locations/{location_id}"
                headers = {
                    'Authorization': f'Bearer {access_token}',
                    'Version': getattr(settings, 'GHL_API_VERSION', '2021-07-28'),
                }
                location_response = requests.get(location_info_url, headers=headers, timeout=30)
                if location_response.status_code == 200:
                    location_info = location_response.json()
                    location_name = location_info.get('name') or location_info.get('companyName')
        except Exception as exc:
            logger.warning("Failed to fetch location name for %s: %s", location_id, exc)
        
        # Save tokens to location
        location, created = GHLLocation.objects.update_or_create(
            location_id=location_id,
            defaults={
                'access_token': token_data.get('access_token', ''),
                'refresh_token': token_data.get('refresh_token', ''),
                'token_expires_at': timezone.now() + timedelta(seconds=token_data.get('expires_in', 3600)),
                'status': 'active',
                'company_name': location_name or '',
                'onboarded_at': timezone.now(),
                'metadata': {
                    **token_data,
                    'scope': token_data.get('scope'),
                    'user_type': token_data.get('userType'),
                    'company_id': token_data.get('companyId'),
                    'user_id': token_data.get('userId'),
                },
            }
        )
        
        logger.info("OAuth tokens saved for location %s (created: %s)", location_id, created)
        
        return Response(
            {
                "message": "Authentication successful",
                "location_id": location_id,
                "location_name": location_name or location.company_name,
                "token_stored": True,
                "note": f"Use this location_id ({location_id}) in your login URL: ?location={location_id}",
            },
            status=status.HTTP_200_OK
        )


class GHLOnboardView(APIView):
    """
    Simple GET endpoint that redirects to GHL OAuth authorization page.
    User will select their location in GHL's interface.
    GET /api/ghlpage/onboard/
    """
    permission_classes = [AllowAny]

    def get(self, request):
        # Build OAuth authorization URL
        client_id = getattr(settings, 'GHL_CLIENT_ID', '')
        redirect_uri = getattr(settings, 'GHL_REDIRECTED_URI', '')
        scope = getattr(settings, 'GHL_SCOPE', '')
        auth_url = getattr(settings, 'GHL_AUTH_URL', '')
        
        if not all([client_id, redirect_uri, scope, auth_url]):
            return Response(
                {"error": "GHL OAuth configuration incomplete"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Build authorization URL (same format as your old code)
        auth_redirect_url = (
            f"{auth_url}?"
            f"response_type=code&"
            f"redirect_uri={redirect_uri}&"
            f"client_id={client_id}&"
            f"scope={scope}"
        )
        
        return redirect(auth_redirect_url)



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def test_contact_custom_fields(request):
    """
    Test endpoint to check custom fields for the current user's contact
    GET /api/ghlpage/test-custom-fields/
    """
    user = request.user
    location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
    
    if not location_id:
        return Response({"error": "No location ID found"}, status=400)
    
    if not user.ghl_contact_id:
        return Response({"error": "No GHL contact ID found for user"}, status=400)
    
    custom_fields = debug_contact_custom_fields(user.ghl_contact_id, location_id)
    
    return Response({
        "user_phone": user.phone,
        "location_id": location_id,
        "contact_id": user.ghl_contact_id,
        "custom_fields": custom_fields
    })

@api_view(['GET'])
@permission_classes([AllowAny])
def test_all_custom_fields(request):
    """
    Test endpoint to list all custom fields for a location
    GET /api/ghlpage/test-all-fields/
    """
    location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
    
    if not location_id:
        return Response({"error": "location_id is required"}, status=400)
    
    from .services import list_contact_custom_fields
    custom_fields = list_contact_custom_fields(location_id)
    
    return Response({
        "location_id": location_id,
        "custom_fields": custom_fields
    })



@api_view(['POST'])
@permission_classes([AllowAny])
def test_otp_custom_field(request):
    """
    Test OTP custom field storage
    POST /api/ghlpage/test-otp-field/
    {
        "phone": "1234567890",
        "location_id": "IAUlKWcfkG3E0IihzMFj"
    }
    """
    phone = request.data.get('phone')
    location_id = request.data.get('location_id')
    
    if not phone or not location_id:
        return Response({"error": "phone and location_id are required"}, status=400)
    
    from users.models import User
    from ghl.services import sync_user_contact, debug_contact_custom_fields
    
    try:
        user = User.objects.get(phone=phone)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    
    # Generate test OTP
    import random
    test_otp = str(random.randint(100000, 999999))
    
    print(f"\n🔐 TEST: Setting OTP {test_otp} for {phone}")
    
    # Sync with GHL
    result, contact_id = sync_user_contact(
        user,
        location_id=location_id,
        custom_fields={
            'login_otp': test_otp,
        },
    )
    
    if contact_id:
        # Check the custom fields after sync
        print(f"🔍 Checking custom fields after OTP sync...")
        custom_fields = debug_contact_custom_fields(contact_id, location_id)
        
        return Response({
            "message": "OTP test completed",
            "phone": phone,
            "contact_id": contact_id,
            "test_otp": test_otp,
            "custom_fields": custom_fields
        })
    else:
        return Response({"error": "Failed to sync with GHL"}, status=500)


@api_view(['POST'])
@permission_classes([AllowAny])
def test_purchase_custom_field(request):
    """
    Test purchase amount custom field storage
    POST /api/ghlpage/test-purchase-field/
    {
        "phone": "1234567890",
        "location_id": "IAUlKWcfkG3E0IihzMFj",
        "amount": 99.99
    }
    """
    phone = request.data.get('phone')
    location_id = request.data.get('location_id')
    amount = request.data.get('amount', 99.99)
    
    if not phone or not location_id:
        return Response({"error": "phone and location_id are required"}, status=400)
    
    from users.models import User
    from ghl.services import sync_user_contact, debug_contact_custom_fields
    
    try:
        user = User.objects.get(phone=phone)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    
    print(f"\n💰 TEST: Setting purchase amount ${amount} for {phone}")
    
    # Sync with GHL
    result, contact_id = sync_user_contact(
        user,
        location_id=location_id,
        custom_fields={
            'purchase_amount': amount,
        },
    )
    
    if contact_id:
        # Check the custom fields after sync
        print(f"🔍 Checking custom fields after purchase sync...")
        custom_fields = debug_contact_custom_fields(contact_id, location_id)
        
        return Response({
            "message": "Purchase test completed",
            "phone": phone,
            "contact_id": contact_id,
            "test_amount": amount,
            "custom_fields": custom_fields
        })
    else:
        return Response({"error": "Failed to sync with GHL"}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_all_ghl_locations(request):
    """
    List all GHL locations for superadmin.
    GET /api/ghlpage/admin/locations/
    """
    # Only superadmin can access this
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can access this endpoint.")
    
    locations = GHLLocation.objects.all().order_by('company_name', 'location_id')
    serializer = GHLLocationSerializer(locations, many=True)
    return Response({
        'locations': serializer.data,
        'count': locations.count(),
    }, status=status.HTTP_200_OK)


@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def update_ghl_location_company_name(request, location_id):
    """
    Update company name for a GHL location.
    Only superadmin can update company names.
    PUT/PATCH /api/ghlpage/admin/locations/<location_id>/company-name/
    Body: {"company_name": "New Company Name"}
    """
    # Only superadmin can access this
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can update company names.")
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
    except GHLLocation.DoesNotExist:
        return Response(
            {'error': f'Location with location_id {location_id} does not exist.'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    company_name = request.data.get('company_name', '').strip()
    location.company_name = company_name
    location.save(update_fields=['company_name', 'updated_at'])
    
    serializer = GHLLocationSerializer(location)
    return Response({
        'message': 'Company name updated successfully.',
        'location': serializer.data
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def set_ghl_location_company_name(request):
    """
    Set company name for a GHL location.
    Only superadmin can set company names.
    POST /api/ghlpage/admin/locations/set-company-name/
    Body: {"location_id": "...", "company_name": "Company Name"}
    """
    # Only superadmin can access this
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can set company names.")
    
    location_id = request.data.get('location_id')
    company_name = request.data.get('company_name', '').strip()
    
    if not location_id:
        return Response(
            {'error': 'location_id is required.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
    except GHLLocation.DoesNotExist:
        return Response(
            {'error': f'Location with location_id {location_id} does not exist.'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    location.company_name = company_name
    location.save(update_fields=['company_name', 'updated_at'])
    
    serializer = GHLLocationSerializer(location)
    return Response({
        'message': 'Company name set successfully.',
        'location': serializer.data
    }, status=status.HTTP_200_OK)

