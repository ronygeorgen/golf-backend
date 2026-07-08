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
from users.permissions import IsActiveLocationMember
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
            'version_id': getattr(settings, 'GHL_VERSION_ID', '69b46b052d4af946d411ad35'),
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
        
        from django.http import HttpResponse

        # HTML Templates for Success and Failure
        def get_html_response(is_success, title, message, details=None):
            status_icon = "✓" if is_success else "✕"
            status_color = "#10b981" if is_success else "#ef4444"
            bg_color = "#f0fdf4" if is_success else "#fef2f2"
            
            return f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>{title}</title>
                <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
                <style>
                    body {{
                        font-family: 'Inter', -apple-system, sans-serif;
                        background-color: #f8fafc;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        height: 100vh;
                        margin: 0;
                        color: #1e293b;
                    }}
                    .card {{
                        background: white;
                        padding: 3rem;
                        border-radius: 1.5rem;
                        box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);
                        text-align: center;
                        max-width: 450px;
                        width: 90%;
                        border: 1px solid #e2e8f0;
                    }}
                    .icon-container {{
                        width: 80px;
                        height: 80px;
                        background-color: {bg_color};
                        color: {status_color};
                        border-radius: 50%;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        font-size: 40px;
                        font-weight: bold;
                        margin: 0 auto 2rem;
                    }}
                    h1 {{ font-size: 1.875rem; font-weight: 700; margin-bottom: 1rem; color: #0f172a; }}
                    p {{ color: #64748b; line-height: 1.625; margin-bottom: 2rem; font-size: 1.125rem; }}
                    .details {{ 
                        background: #f1f5f9; 
                        padding: 1rem; 
                        border-radius: 0.75rem; 
                        font-family: monospace; 
                        font-size: 0.875rem; 
                        color: #475569; 
                        word-break: break-all;
                        margin-bottom: 2rem;
                    }}
                    .btn {{
                        display: inline-block;
                        background: #0f172a;
                        color: white;
                        padding: 0.875rem 2rem;
                        border-radius: 0.75rem;
                        text-decoration: none;
                        font-weight: 600;
                        transition: transform 0.2s, background 0.2s;
                    }}
                    .btn:hover {{ background: #1e293b; transform: translateY(-1px); }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="icon-container">{status_icon}</div>
                    <h1>{title}</h1>
                    <p>{message}</p>
                    {f'<div class="details">{details}</div>' if details else ''}
                    <a href="javascript:window.close()" class="btn">Close Window</a>
                    <div style="margin-top: 1.5rem; font-size: 0.875rem; color: #94a3b8;">
                        You can safely close this tab now.
                    </div>
                </div>
            </body>
            </html>
            """
        
        if not code:
            return HttpResponse(
                get_html_response(False, "Authentication Failed", "Authorization code is required."),
                status=200 # Return 200 so they see the styled error page
            )
        
        # Exchange code for tokens first
        client_id = getattr(settings, 'GHL_CLIENT_ID', '')
        client_secret = getattr(settings, 'GHL_CLIENT_SECRET', '')
        redirect_uri = getattr(settings, 'GHL_REDIRECTED_URI', '')
        base_url = getattr(settings, 'GHL_BASE_URL', 'https://services.leadconnectorhq.com')
        token_url = f"{base_url}/oauth/token"
        
        if not all([client_id, client_secret, redirect_uri]):
            return HttpResponse(
                get_html_response(False, "Config Error", "GHL OAuth configuration is incomplete on the server."),
                status=200
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
        except requests.exceptions.HTTPError as exc:
            error_text = response.text if hasattr(response, 'text') else str(exc)
            logger.error("Failed to exchange OAuth code for tokens: %s - %s", exc, error_text, exc_info=True)
            return HttpResponse(
                get_html_response(False, "Authorization Failed", "Failed to complete the OAuth flow.", error_text),
                status=200
            )
        except Exception as exc:
            logger.error("Failed to exchange OAuth code for tokens: %s", exc, exc_info=True)
            return HttpResponse(
                get_html_response(False, "System Error", "An unexpected error occurred during processing."),
                status=200
            )
        
        if not location_id:
            location_id = token_data.get('locationId')
        
        if not location_id:
            return HttpResponse(
                get_html_response(False, "Integration Error", "Location ID not found in the response from GHL."),
                status=200
            )
        
        # Get location name from GHL API
        location_name = None
        try:
            access_token = token_data.get('access_token')
            if access_token:
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
        
        # Save tokens
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
                },
            }
        )
        
        return HttpResponse(
            get_html_response(True, "Authentication Successful", f"GHL for '{location_name or location_id}' has been successfully integrated."),
            status=200
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
        
        # Build authorization URL
        version_id = getattr(settings, 'GHL_VERSION_ID', '69b46b052d4af946d411ad35')
        auth_redirect_url = (
            f"{auth_url}?"
            f"response_type=code&"
            f"redirect_uri={redirect_uri}&"
            f"client_id={client_id}&"
            f"scope={scope}&"
            f"version_id={version_id}"
        )
        
        return redirect(auth_redirect_url)



@api_view(['GET'])
@permission_classes([IsAuthenticated, IsActiveLocationMember])
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
@permission_classes([IsAuthenticated, IsActiveLocationMember])
def list_all_ghl_locations(request):
    """
    List all GHL locations for superadmin.
    GET /api/ghlpage/admin/locations/
    """
    # Only superadmin can access this
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can access this endpoint.")
    
    locations = GHLLocation.objects.all().order_by('company_name', 'location_id')
    serializer = GHLLocationSerializer(locations, many=True, context={'request': request})
    return Response({
        'locations': serializer.data,
        'count': locations.count(),
    }, status=status.HTTP_200_OK)


@api_view(['PUT', 'PATCH'])
@permission_classes([IsAuthenticated, IsActiveLocationMember])
def update_ghl_location_company_name(request, location_id):
    """
    Update company name and/or timezone for a GHL location.
    Only superadmin can update these settings.
    PUT/PATCH /api/ghlpage/admin/locations/<location_id>/company-name/
    Body: {"company_name": "New Company Name", "timezone": "America/Halifax"}
    """
    # Only superadmin can access this
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can update location settings.")
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
    except GHLLocation.DoesNotExist:
        return Response(
            {'error': f'Location with location_id {location_id} does not exist.'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    update_fields = ['updated_at']
    
    company_name = request.data.get('company_name')
    if company_name is not None:
        location.company_name = company_name.strip()
        update_fields.append('company_name')
    
    timezone_str = request.data.get('timezone')
    if timezone_str is not None:
        # Validate IANA timezone string
        import pytz
        try:
            pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            return Response(
                {'error': f'Invalid timezone: "{timezone_str}". Please use a valid IANA timezone name (e.g. America/Halifax, America/Toronto).'},
                status=status.HTTP_400_BAD_REQUEST
            )
        location.timezone = timezone_str
        update_fields.append('timezone')

    # Invoice contact fields
    contact_phone = request.data.get('contact_phone')
    if contact_phone is not None:
        location.contact_phone = contact_phone.strip()
        update_fields.append('contact_phone')

    support_email = request.data.get('support_email')
    if support_email is not None:
        location.support_email = support_email.strip()
        update_fields.append('support_email')

    business_id = request.data.get('business_id')
    if business_id is not None:
        location.business_id = business_id.strip()
        update_fields.append('business_id')

    refund_policy = request.data.get('refund_policy')
    if refund_policy is not None:
        location.refund_policy = refund_policy.strip()
        update_fields.append('refund_policy')

    tax_rate = request.data.get('tax_rate')
    if tax_rate is not None:
        try:
            from decimal import Decimal
            tax_rate_decimal = Decimal(str(tax_rate))
            if not (0 <= tax_rate_decimal <= 1):
                return Response(
                    {'error': 'tax_rate must be a decimal between 0 and 1 (e.g. 0.14 for 14%).'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            location.tax_rate = tax_rate_decimal
            update_fields.append('tax_rate')
        except Exception:
            return Response(
                {'error': 'Invalid tax_rate value. Must be a number between 0 and 1.'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
    status_val = request.data.get('status')
    if status_val in ['active', 'inactive']:
        location.status = status_val
        update_fields.append('status')
    
    location.save(update_fields=update_fields)
    
    serializer = GHLLocationSerializer(location, context={'request': request})
    return Response({
        'message': 'Location settings updated successfully.',
        'location': serializer.data
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsActiveLocationMember])
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
    
    serializer = GHLLocationSerializer(location, context={'request': request})
    return Response({
        'message': 'Company name set successfully.',
        'location': serializer.data
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsActiveLocationMember])
def upload_ghl_location_logo(request, location_id):
    """
    Upload (or replace) the logo for a GHL location.
    Only superadmin can upload logos.
    POST /api/ghlpage/admin/locations/<location_id>/logo/
    Content-Type: multipart/form-data
    Body: logo=<file>
    Constraints enforced on the *frontend* before upload:
      - Resized to 912×273 px via canvas
      - Max size 1 MB
    The backend also enforces the 1 MB limit as a safety net.
    """
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can upload logos.")
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
    except GHLLocation.DoesNotExist:
        return Response(
            {'error': f'Location {location_id} does not exist.'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    logo_file = request.FILES.get('logo')
    if not logo_file:
        return Response(
            {'error': 'No logo file provided.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Enforce 1 MB size limit
    MAX_SIZE = 1 * 1024 * 1024  # 1 MB
    if logo_file.size > MAX_SIZE:
        return Response(
            {'error': 'Logo file must be smaller than 1 MB.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Validate that it is an image
    allowed_types = ['image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp']
    if logo_file.content_type not in allowed_types:
        return Response(
            {'error': 'Logo must be an image (PNG, JPG, GIF, or WebP).'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Remove the old logo file from disk before saving new one
    if location.logo:
        import os
        old_path = location.logo.path
        if os.path.exists(old_path):
            os.remove(old_path)
    
    location.logo = logo_file
    location.save(update_fields=['logo', 'updated_at'])
    
    serializer = GHLLocationSerializer(location, context={'request': request})
    return Response({
        'message': 'Logo uploaded successfully.',
        'location': serializer.data
    }, status=status.HTTP_200_OK)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated, IsActiveLocationMember])
def delete_ghl_location_logo(request, location_id):
    """
    Delete the logo for a GHL location.
    Only superadmin can delete logos.
    DELETE /api/ghlpage/admin/locations/<location_id>/logo/
    """
    if request.user.role != 'superadmin':
        raise PermissionDenied("Only superadmin can delete logos.")
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
    except GHLLocation.DoesNotExist:
        return Response(
            {'error': f'Location {location_id} does not exist.'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    if not location.logo:
        return Response(
            {'error': 'This location has no logo to delete.'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    import os
    old_path = location.logo.path
    if os.path.exists(old_path):
        os.remove(old_path)
    
    location.logo = None
    location.save(update_fields=['logo', 'updated_at'])
    
    return Response({'message': 'Logo deleted successfully.'}, status=status.HTTP_200_OK)
