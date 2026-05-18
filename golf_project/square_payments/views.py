"""
Square Payment Views
====================

Endpoints
---------
POST /api/square/initiate-payment/
    Frontend calls this with card nonce + temp_id.
    Looks up the client's GHL location → fetches per-location Square token →
    charges the card → finalizes the booking/purchase/event.

POST /api/square/webhook/
    Square calls this for async payment events.
    Single master webhook, routes by metadata.type.

GET  /api/square/config/
    Frontend fetches Application ID + Location ID to init Square Web SDK.
    Now returns per-location data when a location_id is provided.

GET  /api/square/oauth/authorize/
    Redirects golf-center admin to Square OAuth consent page.
    Query param: location_id (required)

GET  /api/square/oauth/callback/
    Square redirects here with ?code=AUTH_CODE&state=GHL_LOCATION_ID.
    Exchanges code for tokens, fetches Square location, stores in DB.

GET  /api/square/oauth/status/<location_id>/
    Returns connection status + Square location info for a GHL location.

POST /api/square/oauth/disconnect/<location_id>/
    Disconnects Square for a GHL location (admin only).
"""
import uuid
import json
import logging

from django.db import transaction, models
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied

from .services import (
    create_payment,
    verify_webhook_signature,
    build_oauth_url,
    exchange_oauth_code,
    fetch_square_locations,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared HTML helper (reuse GHL style)
# ---------------------------------------------------------------------------

def _oauth_html(is_success: bool, title: str, message: str, details: str = '') -> str:
    icon = '✓' if is_success else '✕'
    icon_color = '#10b981' if is_success else '#ef4444'
    bg_color = '#f0fdf4' if is_success else '#fef2f2'
    details_html = f'<div class="details">{details}</div>' if details else ''

    # On success: notify opener and auto-close after 2 s.
    # On failure: show the error, provide a manual close button.
    auto_close_script = """
    <script>
      // Notify the parent window that OAuth completed
      try {
        if (window.opener) {
          window.opener.postMessage({ type: 'square_oauth_complete', success: """ + ('true' if is_success else 'false') + """ }, '*');
        }
      } catch(e) {}
      """ + ("setTimeout(() => window.close(), 2000);" if is_success else "") + """
    </script>""" if is_success else """
    <script>
      try {
        if (window.opener) {
          window.opener.postMessage({ type: 'square_oauth_complete', success: false }, '*');
        }
      } catch(e) {}
    </script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    body {{
      font-family: 'Inter', -apple-system, sans-serif;
      background: #f8fafc;
      display: flex; align-items: center; justify-content: center;
      height: 100vh; margin: 0; color: #1e293b;
    }}
    .card {{
      background: white; padding: 3rem; border-radius: 1.5rem;
      box-shadow: 0 10px 25px -5px rgba(0,0,0,.1);
      text-align: center; max-width: 450px; width: 90%;
      border: 1px solid #e2e8f0;
    }}
    .icon {{
      width: 80px; height: 80px; background: {bg_color}; color: {icon_color};
      border-radius: 50%; display: flex; align-items: center; justify-content: center;
      font-size: 40px; font-weight: bold; margin: 0 auto 2rem;
    }}
    h1 {{ font-size: 1.875rem; font-weight: 700; margin-bottom: 1rem; color: #0f172a; }}
    p {{ color: #64748b; line-height: 1.625; margin-bottom: 2rem; font-size: 1.125rem; }}
    .details {{
      background: #f1f5f9; padding: 1rem; border-radius: .75rem;
      font-family: monospace; font-size: .875rem; color: #475569;
      word-break: break-all; margin-bottom: 2rem;
    }}
    .btn {{
      display: inline-block; background: #0f172a; color: white;
      padding: .875rem 2rem; border-radius: .75rem; text-decoration: none;
      font-weight: 600; transition: background .2s; cursor: pointer; border: none;
      font-size: 1rem;
    }}
    .btn:hover {{ background: #1e293b; }}
    .countdown {{ margin-top: 1rem; font-size: .875rem; color: #94a3b8; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    {details_html}
    <button class="btn" onclick="window.close()">Close Window</button>
    {'<div class="countdown">This window will close automatically in 2 seconds…</div>' if is_success else ''}
  </div>
  {auto_close_script}
</body>
</html>"""


# ===========================================================================
# SQUARE OAUTH VIEWS
# ===========================================================================

class SquareOAuthAuthorizeView(APIView):
    """
    GET /api/square/oauth/authorize/?location_id=<GHL_LOCATION_ID>

    Returns the Square OAuth URL as JSON. The frontend calls this via axios
    (with the Authorization header), receives the URL, then opens it in a
    popup directly. We do NOT server-side-redirect because window.open() sends
    no Authorization header and would get a 401 before the redirect happens.

    Access rules:
    - Superadmin: can connect / reconnect / disconnect any location at any time.
    - Admin: can connect / reconnect / disconnect their own location at any time.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user_role = getattr(request.user, 'role', None)
        is_superadmin = user_role == 'superadmin'
        is_admin = user_role == 'admin' or getattr(request.user, 'is_superuser', False)

        if not is_superadmin and not is_admin:
            raise PermissionDenied("Only admin or superadmin can connect Square accounts.")

        ghl_location_id = request.query_params.get('location_id', '').strip()
        if not ghl_location_id:
            return Response(
                {'error': 'location_id query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify GHL location exists
        from ghl.models import GHLLocation
        if not GHLLocation.objects.filter(location_id=ghl_location_id).exists():
            return Response(
                {'error': f'GHL Location "{ghl_location_id}" not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        oauth_url = build_oauth_url(state=ghl_location_id)
        logger.info(
            "User %s (role=%s) requesting Square OAuth URL for location %s",
            request.user.id, user_role, ghl_location_id,
        )
        # Return the URL as JSON — the frontend opens it in a popup directly.
        return Response({'oauth_url': oauth_url, 'location_id': ghl_location_id})


class SquareOAuthCallbackView(APIView):
    """
    GET /api/square/oauth/callback/?code=AUTH_CODE&state=GHL_LOCATION_ID

    Square redirects here after the golf-center owner approves the app.
    We exchange the code for tokens, fetch the Square location list,
    pick the primary location, and store everything in LocationSquareAccount.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        code = request.query_params.get('code', '').strip()
        ghl_location_id = request.query_params.get('state', '').strip()
        error_param = request.query_params.get('error', '').strip()

        # --- User denied access ---
        if error_param:
            logger.warning("Square OAuth denied for location %s: %s", ghl_location_id, error_param)
            return HttpResponse(
                _oauth_html(False, 'Access Denied', 'You denied access to Square. Please try again.'),
                content_type='text/html',
            )

        if not code:
            return HttpResponse(
                _oauth_html(False, 'Authorization Failed', 'No authorization code received from Square.'),
                content_type='text/html',
            )

        if not ghl_location_id:
            return HttpResponse(
                _oauth_html(False, 'Configuration Error', 'Location ID (state) is missing from the callback.'),
                content_type='text/html',
            )

        # --- Fetch GHL Location ---
        from ghl.models import GHLLocation
        try:
            ghl_location = GHLLocation.objects.get(location_id=ghl_location_id)
        except GHLLocation.DoesNotExist:
            return HttpResponse(
                _oauth_html(False, 'Not Found', f'GHL Location "{ghl_location_id}" not found in the system.'),
                content_type='text/html',
            )

        # --- Exchange code for tokens ---
        try:
            token_data = exchange_oauth_code(code)
        except ValueError as exc:
            logger.error("Square OAuth exchange failed for location %s: %s", ghl_location_id, exc)
            return HttpResponse(
                _oauth_html(False, 'Authorization Failed', 'Failed to exchange authorization code with Square.', str(exc)),
                content_type='text/html',
            )

        access_token = token_data.get('access_token', '')
        refresh_token = token_data.get('refresh_token', '')
        merchant_id = token_data.get('merchant_id', '')
        expires_at_str = token_data.get('expires_at', '')  # ISO 8601

        # Parse expires_at from Square's ISO string
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime
        token_expires_at = None
        if expires_at_str:
            try:
                token_expires_at = parse_datetime(expires_at_str)
                if token_expires_at and not token_expires_at.tzinfo:
                    import datetime
                    token_expires_at = token_expires_at.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                # Fallback: access tokens valid for 30 days
                token_expires_at = timezone.now() + timezone.timedelta(days=30)
        if not token_expires_at:
            token_expires_at = timezone.now() + timezone.timedelta(days=30)

        # --- Fetch Square locations ---
        square_locations = fetch_square_locations(access_token)
        # Pick the first ACTIVE location (or just the first one)
        sq_location_id = ''
        sq_location_name = ''
        if square_locations:
            active = [l for l in square_locations if l.get('status') == 'ACTIVE']
            chosen = active[0] if active else square_locations[0]
            sq_location_id = chosen.get('id', '')
            sq_location_name = chosen.get('name', '')

        # --- Save / update LocationSquareAccount ---
        from .models import LocationSquareAccount
        account, created = LocationSquareAccount.objects.update_or_create(
            ghl_location=ghl_location,
            defaults={
                'merchant_id': merchant_id,
                'access_token': access_token,
                'refresh_token': refresh_token,
                'token_expires_at': token_expires_at,
                'square_location_id': sq_location_id,
                'square_location_name': sq_location_name,
                'is_connected': True,
                'connected_at': timezone.now(),
            }
        )

        company_name = ghl_location.company_name or ghl_location_id
        action = 'connected' if created else 'reconnected'
        logger.info(
            "Square OAuth %s for location %s (merchant_id=%s, sq_location=%s)",
            action, ghl_location_id, merchant_id, sq_location_id,
        )

        return HttpResponse(
            _oauth_html(
                True,
                'Square Connected!',
                f"Square account for <strong>{company_name}</strong> has been successfully "
                f"connected. Payments from this golf center will now go directly to their Square account.",
                f"Merchant ID: {merchant_id} | Square Location: {sq_location_name or sq_location_id}",
            ),
            content_type='text/html',
        )


class SquareOAuthStatusView(APIView):
    """
    GET /api/square/oauth/status/<ghl_location_id>/

    Returns the Square connection status for a GHL location.
    Accessible by authenticated users (staff/admin check in frontend).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, ghl_location_id):
        from .models import LocationSquareAccount
        from django.utils import timezone

        try:
            account = LocationSquareAccount.objects.select_related('ghl_location').get(
                ghl_location__location_id=ghl_location_id,
            )
        except LocationSquareAccount.DoesNotExist:
            return Response({
                'is_connected': False,
                'ghl_location_id': ghl_location_id,
                'message': 'Square has not been connected for this location.',
            })

        return Response({
            'is_connected': account.is_connected,
            'ghl_location_id': ghl_location_id,
            'merchant_id': account.merchant_id,
            'square_location_id': account.square_location_id,
            'square_location_name': account.square_location_name,
            'token_valid': account.is_token_valid(),
            'token_expires_at': account.token_expires_at,
            'connected_at': account.connected_at,
        })


class SquareOAuthDisconnectView(APIView):
    """
    POST /api/square/oauth/disconnect/<ghl_location_id>/

    Admin or superadmin can disconnect Square for a location.
    Clears tokens but keeps the record for auditing.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, ghl_location_id):
        user_role = getattr(request.user, 'role', None)
        if user_role not in ('superadmin', 'admin') and not getattr(request.user, 'is_superuser', False):
            raise PermissionDenied("Only admin or superadmin can disconnect Square accounts.")

        from .models import LocationSquareAccount

        try:
            account = LocationSquareAccount.objects.get(
                ghl_location__location_id=ghl_location_id,
            )
        except LocationSquareAccount.DoesNotExist:
            return Response(
                {'error': f'No Square account found for location "{ghl_location_id}".'},
                status=status.HTTP_404_NOT_FOUND,
            )

        account.access_token = ''
        account.refresh_token = ''
        account.token_expires_at = None
        account.is_connected = False
        account.save(update_fields=['access_token', 'refresh_token', 'token_expires_at', 'is_connected', 'updated_at'])
        logger.info("Square disconnected for location %s by %s (role=%s)", ghl_location_id, request.user.id, user_role)

        return Response({'message': 'Square account disconnected successfully.'})


class SquareOAuthListView(APIView):
    """
    GET /api/square/oauth/list/

    Returns ALL GHL locations with their Square connection status
    (superadmin only).

    Unlike the old version that only returned locations with an existing
    LocationSquareAccount row, this LEFT JOINs all GHL locations so every
    location appears — connected or not — allowing the superadmin to action
    any location directly from the table.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.role != 'superadmin':
            raise PermissionDenied("Only superadmin can list Square connections.")

        from ghl.models import GHLLocation
        from .models import LocationSquareAccount

        # Fetch all known GHL locations
        all_locations = GHLLocation.objects.all().order_by('company_name', 'location_id')

        # Build a lookup dict: ghl_location_id → LocationSquareAccount (or None)
        sq_map = {
            acc.ghl_location_id: acc
            for acc in LocationSquareAccount.objects.select_related('ghl_location').all()
        }

        data = []
        for loc in all_locations:
            acc = sq_map.get(loc.id)           # acc is None when no record exists yet
            data.append({
                'ghl_location_id': loc.location_id,
                'company_name': loc.company_name or '',
                'is_connected': acc.is_connected if acc else False,
                'merchant_id': acc.merchant_id if acc else '',
                'square_location_id': acc.square_location_id if acc else '',
                'square_location_name': acc.square_location_name if acc else '',
                'token_valid': acc.is_token_valid() if acc else False,
                'token_expires_at': acc.token_expires_at if acc else None,
                'connected_at': acc.connected_at if acc else None,
            })

        # Sort: connected-valid first, then expired, then not connected
        def _sort_key(row):
            if row['is_connected'] and row['token_valid']:
                return 0
            if row['is_connected'] and not row['token_valid']:
                return 1
            return 2

        data.sort(key=_sort_key)

        return Response({'connections': data, 'count': len(data)})


# ===========================================================================
# HELPER: resolve per-location Square credentials from a temp booking
# ===========================================================================

def _resolve_square_credentials(temp_id_str: str, payment_type: str):
    """
    Given a temp_id and payment_type, find the GHL location_id from the
    temp booking, then look up the LocationSquareAccount to get the
    per-location access_token + square_location_id.

    Returns: (access_token: str, square_location_id: str)

    Raises:
        ValueError: If the location has not connected a Square account.
                    No fallback to the platform/global token — every location
                    must connect their own Square account.
    """
    ghl_location_id = None

    try:
        if payment_type in ('simulator', 'asset') or (payment_type and payment_type.startswith('asset:')):
            from bookings.models import TempBooking
            tb = TempBooking.objects.filter(temp_id=temp_id_str).first()
            if tb:
                ghl_location_id = tb.location_id
                if not ghl_location_id:
                    from users.models import User
                    buyer = User.objects.filter(phone=tb.buyer_phone).first()
                    ghl_location_id = getattr(buyer, 'ghl_location_id', None) if buyer else None
        elif payment_type == 'package':
            from coaching.models import TempPurchase
            tp = TempPurchase.objects.filter(temp_id=temp_id_str).first()
            if tp:
                from users.models import User
                buyer = User.objects.filter(phone=tp.buyer_phone).first()
                ghl_location_id = getattr(buyer, 'ghl_location_id', None) if buyer else None
        elif payment_type == 'event':
            from special_events.models import TempEventRegistration
            ter = TempEventRegistration.objects.filter(temp_id=temp_id_str).first()
            if ter:
                from users.models import User
                buyer = User.objects.filter(phone=ter.phone).first()
                ghl_location_id = getattr(buyer, 'ghl_location_id', None) if buyer else None
    except Exception as exc:
        logger.warning("_resolve_square_credentials: error resolving location: %s", exc)

    if not ghl_location_id:
        raise ValueError(
            "Cannot determine the golf center location for this booking. "
            "Payment cannot be processed."
        )

    from .models import LocationSquareAccount
    try:
        account = LocationSquareAccount.objects.get(
            ghl_location__location_id=ghl_location_id,
            is_connected=True,
        )
    except LocationSquareAccount.DoesNotExist:
        raise ValueError(
            f"This golf center has not connected a Square account yet. "
            "Please contact the golf center to set up online payments."
        )

    logger.info(
        "Using per-location Square token for location %s (merchant=%s)",
        ghl_location_id, account.merchant_id,
    )
    return account.access_token, account.square_location_id


# ===========================================================================
# BOOKING FINALIZATION HELPERS (unchanged logic)
# ===========================================================================

def _finalize_simulator_booking(temp_id_str: str, payment_id: str):
    """Look up TempBooking by temp_id and convert it into a real Booking."""
    from bookings.models import TempBooking, Booking
    from bookings.serializers import BookingSerializer
    from simulators.models import Simulator
    from users.models import User
    from django.utils import timezone

    temp_id = uuid.UUID(temp_id_str)
    temp_booking = TempBooking.objects.select_for_update().get(temp_id=temp_id)

    if temp_booking.status == 'completed':
        existing = Booking.objects.filter(
            client__phone=temp_booking.buyer_phone,
            start_time=temp_booking.start_time,
            end_time=temp_booking.end_time,
            booking_type='simulator'
        ).order_by('-created_at')[:getattr(temp_booking, 'simulator_count', 1)]
        return {'already_processed': True, 'booking_ids': [b.id for b in existing]}

    if temp_booking.is_expired:
        temp_booking.status = 'expired'
        temp_booking.save(update_fields=['status'])
        raise ValueError('Temporary booking has expired.')

    buyer = User.objects.get(phone=temp_booking.buyer_phone)
    simulator_count = getattr(temp_booking, 'simulator_count', 1)
    location_id = temp_booking.location_id or getattr(buyer, 'ghl_location_id', None)

    active_simulators = Simulator.objects.filter(is_active=True, is_coaching_bay=False)
    if location_id:
        active_simulators = active_simulators.filter(location_id=location_id)
    active_simulators = active_simulators.select_for_update().order_by('bay_number')

    available_simulators = []
    for sim in active_simulators:
        if len(available_simulators) >= simulator_count:
            break
        conflict = Booking.objects.select_for_update().filter(
            simulator=sim,
            start_time__lt=temp_booking.end_time,
            end_time__gt=temp_booking.start_time,
            status__in=['confirmed', 'completed'],
        ).exists()
        if not conflict:
            from bookings.models import TempBooking as TB
            temp_conflict = TB.objects.select_for_update().filter(
                simulator=sim,
                start_time__lt=temp_booking.end_time,
                end_time__gt=temp_booking.start_time,
                status='reserved',
                expires_at__gt=timezone.now()
            ).exclude(temp_id=temp_id).exists()
            if not temp_conflict:
                available_simulators.append(sim)

    if len(available_simulators) < simulator_count:
        temp_booking.status = 'cancelled'
        temp_booking.save(update_fields=['status'])
        raise ValueError(f'Only {len(available_simulators)} simulator(s) available. Slot may have been taken.')

    single_price = temp_booking.total_price / simulator_count
    created_bookings = []
    for sim in available_simulators:
        b = Booking.objects.create(
            client=buyer,
            location_id=location_id,
            booking_type='simulator',
            simulator=sim,
            start_time=temp_booking.start_time,
            end_time=temp_booking.end_time,
            duration_minutes=temp_booking.duration_minutes,
            total_price=single_price,
            status='confirmed'
        )
        created_bookings.append(b)

    temp_booking.payment_id = payment_id
    temp_booking.status = 'completed'
    temp_booking.processed_at = timezone.now()
    temp_booking.save(update_fields=['payment_id', 'status', 'processed_at'])

    try:
        from ghl.tasks import update_user_ghl_custom_fields_task
        update_user_ghl_custom_fields_task.delay(buyer.id, location_id=location_id)
    except Exception as exc:
        logger.warning("Failed to queue GHL update after Square simulator booking: %s", exc)

    booking_serializer = BookingSerializer(created_bookings, many=True)
    logger.info("Square: Simulator booking(s) created: %s", [b.id for b in created_bookings])
    return {'booking_ids': [b.id for b in created_bookings], 'bookings': booking_serializer.data}


def _finalize_asset_booking(temp_id_str: str, payment_id: str):
    """Look up TempBooking by temp_id and convert it into a real Booking for generic asset."""
    from bookings.models import TempBooking, Booking
    from bookings.serializers import BookingSerializer
    from users.models import User
    from django.utils import timezone

    temp_id = uuid.UUID(temp_id_str)
    temp_booking = TempBooking.objects.select_for_update().get(temp_id=temp_id)

    if temp_booking.status == 'completed':
        existing = Booking.objects.filter(
            client__phone=temp_booking.buyer_phone,
            start_time=temp_booking.start_time,
            end_time=temp_booking.end_time,
            category_asset=temp_booking.category_asset
        ).first()
        return {'already_processed': True, 'booking_id': existing.id if existing else None}

    if temp_booking.is_expired:
        temp_booking.status = 'expired'
        temp_booking.save(update_fields=['status'])
        raise ValueError('Temporary booking has expired.')

    buyer = User.objects.get(phone=temp_booking.buyer_phone)
    location_id = temp_booking.location_id or getattr(buyer, 'ghl_location_id', None)

    conflict = Booking.objects.filter(
        category_asset=temp_booking.category_asset,
        start_time__lt=temp_booking.end_time,
        end_time__gt=temp_booking.start_time,
        status__in=['confirmed', 'completed'],
    ).exists()

    if conflict:
        temp_booking.status = 'cancelled'
        temp_booking.save(update_fields=['status'])
        raise ValueError('This asset slot has already been taken by another booking.')

    booking = Booking.objects.create(
        client=buyer,
        location_id=location_id,
        booking_type='coaching',
        service_category=temp_booking.service_category,
        category_asset=temp_booking.category_asset,
        start_time=temp_booking.start_time,
        end_time=temp_booking.end_time,
        duration_minutes=temp_booking.duration_minutes,
        total_price=temp_booking.total_price,
        status='confirmed'
    )

    temp_booking.payment_id = payment_id
    temp_booking.status = 'completed'
    temp_booking.processed_at = timezone.now()
    temp_booking.save(update_fields=['payment_id', 'status', 'processed_at'])

    try:
        from ghl.tasks import update_user_ghl_custom_fields_task
        update_user_ghl_custom_fields_task.delay(buyer.id, location_id=location_id)
    except Exception as exc:
        logger.warning("Failed to queue GHL update after Square asset booking: %s", exc)

    booking_serializer = BookingSerializer(booking)
    logger.info("Square: Asset booking created: %s", booking.id)
    return {'booking_id': booking.id, 'booking': booking_serializer.data}


def _finalize_package_purchase(temp_id_str: str, payment_id: str):
    """Route to the existing PackagePurchaseWebhookView logic."""
    from django.test import RequestFactory
    from rest_framework.request import Request as DRFRequest
    from rest_framework.parsers import JSONParser
    import json as _json

    factory = RequestFactory()
    wsgi_request = factory.post(
        '/',
        data=_json.dumps({'recipient_phone': temp_id_str}),
        content_type='application/json'
    )
    request = DRFRequest(wsgi_request, parsers=[JSONParser()])

    from coaching.views import PackagePurchaseWebhookView
    view = PackagePurchaseWebhookView()
    response = view.post(request)

    if response.status_code not in (200, 201):
        raise ValueError(response.data.get('error', 'Package purchase finalization failed.'))
    return response.data


def _finalize_event_registration(temp_id_str: str, payment_id: str):
    """Route to the existing SpecialEventWebhookView logic."""
    from django.test import RequestFactory
    from rest_framework.request import Request as DRFRequest
    from rest_framework.parsers import JSONParser
    import json as _json

    factory = RequestFactory()
    wsgi_request = factory.post(
        '/',
        data=_json.dumps({'recipient_phone': temp_id_str}),
        content_type='application/json'
    )
    request = DRFRequest(wsgi_request, parsers=[JSONParser()])

    from special_events.views import SpecialEventWebhookView
    view = SpecialEventWebhookView()
    response = view.post(request)

    if response.status_code not in (200, 201):
        raise ValueError(response.data.get('error', 'Event registration finalization failed.'))
    return response.data


# ===========================================================================
# PAYMENT VIEW
# ===========================================================================

class InitiateSquarePaymentView(APIView):
    """
    POST /api/square/initiate-payment/

    Body:
      {
        "source_id": "<nonce from Square Web SDK>",
        "temp_id": "<UUID>",
        "payment_type": "simulator" | "package" | "event" | "asset",
        "amount": 45.00,
        "currency": "CAD",
        "idempotency_key": "<optional UUID>",
        "coupon_code": "<optional>"
      }

    The view:
      1. Resolves the GHL location from the temp booking.
      2. Looks up that location's Square access_token from LocationSquareAccount.
      3. Charges the card using that token → money goes to client's Square.
      4. Finalizes the booking.
    """
    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        source_id = request.data.get('source_id')
        temp_id_str = request.data.get('temp_id')
        payment_type = request.data.get('payment_type')
        amount = request.data.get('amount')
        currency = request.data.get('currency', 'CAD')
        idempotency_key = request.data.get('idempotency_key') or str(uuid.uuid4())
        coupon_code = (request.data.get('coupon_code') or '').strip().upper()

        if not source_id:
            return Response({'error': 'source_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not temp_id_str:
            return Response({'error': 'temp_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if payment_type not in ('simulator', 'package', 'event', 'asset') and not (payment_type and payment_type.startswith('asset:')):
            return Response({'error': 'payment_type must be simulator, package, event, asset, or asset:ID.'}, status=status.HTTP_400_BAD_REQUEST)
        if amount is None:
            return Response({'error': 'amount is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # ── Resolve Item Label and Guest Info ────────────────────────────────
        item_label = ''
        guest_phone = ''
        guest_email = ''

        try:
            if payment_type == 'simulator':
                item_label = 'Simulator Booking'
                from bookings.models import TempBooking
                tb = TempBooking.objects.filter(temp_id=temp_id_str).first()
                if tb:
                    guest_phone = tb.buyer_phone
            elif payment_type == 'package':
                from coaching.models import TempPurchase
                tp = TempPurchase.objects.filter(temp_id=temp_id_str).first()
                if tp:
                    guest_phone = tp.buyer_phone
                    item_label = tp.package.title if tp.package else (tp.simulator_package.title if tp.simulator_package else 'Package Purchase')
            elif payment_type == 'event':
                from special_events.models import TempEventRegistration
                ter = TempEventRegistration.objects.filter(temp_id=temp_id_str).first()
                if ter:
                    guest_phone = ter.phone
                    guest_email = ter.email
                    if ter.occurrence:
                        item_label = f"Event: {ter.occurrence.event.title}"
            elif payment_type == 'asset' or (payment_type and payment_type.startswith('asset:')):
                from bookings.models import TempBooking
                tb = TempBooking.objects.filter(temp_id=temp_id_str).first()
                if tb:
                    guest_phone = tb.buyer_phone
                    item_label = f"Asset: {tb.category_asset.name}" if tb.category_asset else 'Generic Asset Booking'
        except Exception as exc:
            logger.warning("Failed to resolve item_label or guest info: %s", exc)

        try:
            original_amount = float(amount)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid amount.'}, status=status.HTTP_400_BAD_REQUEST)

        coupon_obj = None
        discount_amount = 0.0
        final_amount = original_amount

        if coupon_code:
            from coupons.models import Coupon, CouponUsage
            try:
                coupon_obj = Coupon.objects.select_for_update().get(code=coupon_code)
            except Coupon.DoesNotExist:
                return Response({'error': f'Coupon "{coupon_code}" is invalid.'}, status=status.HTTP_400_BAD_REQUEST)

            is_auth = request.user.is_authenticated
            user_obj = request.user if is_auth else None
            email = getattr(request.user, 'email', None) or guest_email
            phone = getattr(request.user, 'phone', None) or guest_phone

            valid, err = coupon_obj.is_valid(payment_type=payment_type, user=user_obj, email=email, phone=phone)
            if not valid:
                return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

            discount_amount = coupon_obj.calculate_discount(original_amount)
            final_amount = round(original_amount - discount_amount, 2)
            logger.info("Coupon %s applied: -%s → final=%s", coupon_code, discount_amount, final_amount)

        # ── Resolve per-location Square credentials ──────────────────────────
        try:
            sq_access_token, sq_location_id = _resolve_square_credentials(temp_id_str, payment_type)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

        # ── Charge Square ────────────────────────────────────────────────────
        amount_cents = int(round(final_amount * 100))
        if amount_cents <= 0:
            return Response({'error': 'Fully discounted payments are not yet supported.'}, status=status.HTTP_400_BAD_REQUEST)

        metadata = {
            'temp_id': temp_id_str,
            'payment_type': payment_type,
            'customer_phone': getattr(request.user, 'phone', None) or guest_phone or '',
        }

        try:
            payment = create_payment(
                source_id=source_id,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
                note=f"Golf booking ({payment_type}){' | coupon:' + coupon_code if coupon_code else ''}",
                metadata=metadata,
                access_token=sq_access_token,
                location_id=sq_location_id,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        except Exception as exc:
            logger.error("Unexpected Square error: %s", exc, exc_info=True)
            return Response({'error': 'Payment processing failed. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        payment_id = payment.id

        # ── Record coupon usage ───────────────────────────────────────────────
        if coupon_obj:
            from coupons.models import CouponUsage
            CouponUsage.objects.create(
                coupon=coupon_obj,
                user=request.user if request.user.is_authenticated else None,
                customer_email=getattr(request.user, 'email', None) or guest_email or None,
                customer_phone=getattr(request.user, 'phone', None) or guest_phone or None,
                payment_id=payment_id,
                payment_type=payment_type,
                discount_amount=discount_amount,
                original_amount=original_amount,
                final_amount=final_amount,
                item_label=item_label,
            )
            from coupons.models import Coupon
            Coupon.objects.filter(pk=coupon_obj.pk).update(uses_count=models.F('uses_count') + 1)
            logger.info("CouponUsage recorded: %s, payment=%s", coupon_code, payment_id)

        # ── Finalize booking/purchase/event ──────────────────────────────────
        try:
            if payment_type == 'simulator':
                result = _finalize_simulator_booking(temp_id_str, payment_id)
            elif payment_type == 'package':
                result = _finalize_package_purchase(temp_id_str, payment_id)
            elif payment_type == 'asset' or (payment_type and payment_type.startswith('asset:')):
                result = _finalize_asset_booking(temp_id_str, payment_id)
            else:
                result = _finalize_event_registration(temp_id_str, payment_id)
        except Exception as exc:
            logger.error(
                "CRITICAL: Square payment %s succeeded but finalization failed! "
                "temp_id=%s, type=%s, error=%s",
                payment_id, temp_id_str, payment_type, exc,
                exc_info=True,
            )
            return Response(
                {
                    'error': (
                        f'Payment was processed but booking confirmation failed: {str(exc)}. '
                        f'Please contact support with your payment reference: {payment_id}'
                    ),
                    'payment_id': payment_id,
                    'payment_status': 'paid',
                    'booking_status': 'failed',
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            'message': 'Payment successful.',
            'payment_id': payment_id,
            'payment_status': payment.status if hasattr(payment, 'status') else 'paid',
            'booking_status': 'confirmed',
            'coupon_applied': coupon_code or None,
            'discount_amount': discount_amount,
            'result': result,
        }, status=status.HTTP_201_CREATED)


# ===========================================================================
# WEBHOOK
# ===========================================================================

class SquareWebhookView(APIView):
    """
    POST /api/square/webhook/

    Square calls this for async payment events.
    For multi-tenant we still receive all merchant webhooks here and route
    by metadata.type.  The merchant_id in the payload can be cross-referenced
    with LocationSquareAccount.merchant_id if needed.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw_body = request.body
        signature_header = request.headers.get('x-square-hmacsha256-signature', '')
        signature_key = getattr(settings, 'SQUARE_WEBHOOK_SIGNATURE_KEY', '').strip()

        if signature_key:
            notification_url = (
                getattr(settings, 'SQUARE_WEBHOOK_URL', '').strip()
                or request.build_absolute_uri()
            )
            if not verify_webhook_signature(raw_body, signature_header, signature_key, notification_url):
                return Response({'error': 'Invalid signature.'}, status=status.HTTP_401_UNAUTHORIZED)
        else:
            logger.warning("SQUARE_WEBHOOK_SIGNATURE_KEY not set — skipping signature verification.")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response({'error': 'Invalid JSON.'}, status=status.HTTP_400_BAD_REQUEST)

        event_type = payload.get('type', '')
        logger.info("Square webhook received: event_type=%s", event_type)

        # Square V2 uses payment.created and payment.updated.
        # payment.created  → status APPROVED  (skip, money not captured yet)
        # payment.updated  → status COMPLETED (finalize booking)
        if event_type not in ['payment.created', 'payment.updated']:
            return Response({'message': f'Event type {event_type} not handled.'}, status=status.HTTP_200_OK)

        payment_obj = payload.get('data', {}).get('object', {}).get('payment', {})
        payment_id = payment_obj.get('id')
        payment_status = payment_obj.get('status', '')
        payment_metadata = payment_obj.get('metadata', {})
        temp_id_str = payment_metadata.get('temp_id') or payment_obj.get('reference_id')
        payment_type = payment_metadata.get('payment_type')

        # Only finalize if the payment has actually reached the COMPLETED state
        if payment_status != 'COMPLETED':
            logger.info("Square webhook: payment %s is in status %s. Skipping finalization.", payment_id, payment_status)
            return Response({'message': f'Status {payment_status} is not COMPLETED.'}, status=status.HTTP_200_OK)

        logger.info(
            "Square webhook processing: payment_id=%s, temp_id=%s, type=%s",
            payment_id, temp_id_str, payment_type,
        )

        if not temp_id_str or not payment_type:
            logger.warning("Square webhook missing metadata: payment_id=%s", payment_id)
            return Response({'message': 'Missing metadata, skipping.'}, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                if payment_type == 'simulator':
                    _finalize_simulator_booking(temp_id_str, payment_id)
                elif payment_type == 'package':
                    _finalize_package_purchase(temp_id_str, payment_id)
                elif payment_type == 'asset' or (payment_type and payment_type.startswith('asset:')):
                    _finalize_asset_booking(temp_id_str, payment_id)
                elif payment_type == 'event':
                    _finalize_event_registration(temp_id_str, payment_id)
                else:
                    logger.warning("Square webhook: unknown payment_type=%s", payment_type)
        except Exception as exc:
            logger.error("Square webhook finalization error: %s", exc, exc_info=True)

        return Response({'message': 'Webhook received.'}, status=status.HTTP_200_OK)


# ===========================================================================
# CONFIG VIEW
# ===========================================================================

class SquareConfigView(APIView):
    """
    GET /api/square/config/?location_id=<GHL_LOCATION_ID>

    Returns the Square Application ID + Location ID for the frontend Web SDK.

    If location_id is provided and has a connected Square account, returns
    that location's square_location_id.  Otherwise falls back to global default.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        ghl_location_id = request.query_params.get('location_id', '').strip()

        app_id = settings.SQUARE_APPLICATION_ID
        env = settings.SQUARE_ENVIRONMENT

        # Try to resolve per-location square_location_id
        resolved_location_id = settings.SQUARE_LOCATION_ID
        if ghl_location_id:
            try:
                from .models import LocationSquareAccount
                account = LocationSquareAccount.objects.get(
                    ghl_location__location_id=ghl_location_id,
                    is_connected=True,
                )
                resolved_location_id = account.square_location_id or settings.SQUARE_LOCATION_ID
            except LocationSquareAccount.DoesNotExist:
                pass  # Fall back to global

        logger.info(
            "Square config requested. app_id=%s..., env=%s, location_id=%s",
            app_id[:10], env, resolved_location_id,
        )
        return Response({
            'application_id': app_id,
            'location_id': resolved_location_id,
            'environment': env,
        })
