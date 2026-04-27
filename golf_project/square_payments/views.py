"""
Square Payment Views

Provides two endpoints:
1. POST /api/square/initiate-payment/   - The frontend calls this with source_id (nonce) + temp_id.
                                          We charge the card and finalize the booking/purchase/event.
2. POST /api/square/webhook/            - Square calls this after async payment events.
                                          Acts as a single master webhook, routing by metadata.type.
"""
import uuid
import json
import logging

from django.db import transaction, models
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .services import create_payment, verify_webhook_signature

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: finalize a simulator booking (called from both initiate and webhook)
# ---------------------------------------------------------------------------
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
            temp_conflict = TempBooking.objects.select_for_update().filter(
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
    logger.info(f"Square: Simulator booking(s) created: {[b.id for b in created_bookings]}")
    return {'booking_ids': [b.id for b in created_bookings], 'bookings': booking_serializer.data}


# ---------------------------------------------------------------------------
# Helper: finalize a package purchase
# ---------------------------------------------------------------------------
def _finalize_package_purchase(temp_id_str: str, payment_id: str):
    """Route to the existing PackagePurchaseWebhookView logic."""
    from django.test import RequestFactory
    from rest_framework.request import Request as DRFRequest
    from rest_framework.parsers import JSONParser

    factory = RequestFactory()
    wsgi_request = factory.post(
        '/',
        data=json.dumps({'recipient_phone': temp_id_str}),
        content_type='application/json'
    )
    # Wrap in DRF Request so .data works
    request = DRFRequest(wsgi_request, parsers=[JSONParser()])

    from coaching.views import PackagePurchaseWebhookView
    view = PackagePurchaseWebhookView()
    response = view.post(request)

    if response.status_code not in (200, 201):
        raise ValueError(response.data.get('error', 'Package purchase finalization failed.'))
    return response.data


# ---------------------------------------------------------------------------
# Helper: finalize a special event registration
# ---------------------------------------------------------------------------
def _finalize_event_registration(temp_id_str: str, payment_id: str):
    """Route to the existing SpecialEventWebhookView logic."""
    from django.test import RequestFactory
    from rest_framework.request import Request as DRFRequest
    from rest_framework.parsers import JSONParser

    factory = RequestFactory()
    wsgi_request = factory.post(
        '/',
        data=json.dumps({'recipient_phone': temp_id_str}),
        content_type='application/json'
    )
    # Wrap in DRF Request so .data works
    request = DRFRequest(wsgi_request, parsers=[JSONParser()])

    from special_events.views import SpecialEventWebhookView
    view = SpecialEventWebhookView()
    response = view.post(request)

    if response.status_code not in (200, 201):
        raise ValueError(response.data.get('error', 'Event registration finalization failed.'))
    return response.data


# ---------------------------------------------------------------------------
# Main view: Frontend calls this with card nonce + temp_id
# ---------------------------------------------------------------------------
class InitiateSquarePaymentView(APIView):
    """
    POST /api/square/initiate-payment/

    Body:
      {
        "source_id": "<nonce from Square Web SDK>",
        "temp_id": "<UUID>",
        "payment_type": "simulator" | "package" | "event",
        "amount": 45.00,
        "currency": "CAD",
        "idempotency_key": "<optional UUID>"
      }
    """
    permission_classes = [IsAuthenticated]

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
        if payment_type not in ('simulator', 'package', 'event'):
            return Response({'error': 'payment_type must be simulator, package, or event.'}, status=status.HTTP_400_BAD_REQUEST)
        if amount is None:
            return Response({'error': 'amount is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            original_amount = float(amount)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid amount.'}, status=status.HTTP_400_BAD_REQUEST)

        # ── Coupon validation ────────────────────────────────────────────────
        coupon_obj = None
        discount_amount = 0.0
        final_amount = original_amount

        if coupon_code:
            from coupons.models import Coupon, CouponUsage
            try:
                coupon_obj = Coupon.objects.select_for_update().get(code=coupon_code)
            except Coupon.DoesNotExist:
                return Response({'error': f'Coupon "{coupon_code}" is invalid.'}, status=status.HTTP_400_BAD_REQUEST)

            email = getattr(request.user, 'email', None)
            phone = getattr(request.user, 'phone', None)
            valid, err = coupon_obj.is_valid(payment_type=payment_type, user=request.user, email=email, phone=phone)
            if not valid:
                return Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

            discount_amount = coupon_obj.calculate_discount(original_amount)
            final_amount = round(original_amount - discount_amount, 2)
            logger.info(f"Coupon {coupon_code} applied: -{discount_amount} → final={final_amount}")

        # ── Charge Square ────────────────────────────────────────────────────
        amount_cents = int(round(final_amount * 100))
        if amount_cents <= 0:
            amount_cents = 0  # Fully covered by coupon — skip Square charge if needed
            # For now, guard against $0 charge (Square requires > 0)
            if amount_cents == 0:
                return Response({'error': 'Fully discounted payments are not yet supported.'}, status=status.HTTP_400_BAD_REQUEST)

        metadata = {
            'temp_id': temp_id_str,
            'payment_type': payment_type,
            'customer_phone': getattr(request.user, 'phone', ''),
        }

        try:
            payment = create_payment(
                source_id=source_id,
                amount_cents=amount_cents,
                currency=currency,
                idempotency_key=idempotency_key,
                note=f"Golf booking ({payment_type}){' | coupon:' + coupon_code if coupon_code else ''}",
                metadata=metadata,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
        except Exception as exc:
            logger.error(f"Unexpected Square error: {exc}", exc_info=True)
            return Response({'error': 'Payment processing failed. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        payment_id = payment.id

        # ── Record coupon usage ──────────────────────────────────────────────
        if coupon_obj:
            from coupons.models import CouponUsage
            CouponUsage.objects.create(
                coupon=coupon_obj,
                user=request.user,
                customer_email=getattr(request.user, 'email', None),
                customer_phone=getattr(request.user, 'phone', None),
                payment_id=payment_id,
                payment_type=payment_type,
                discount_amount=discount_amount,
                original_amount=original_amount,
                final_amount=final_amount,
            )
            # Increment usage counter atomically
            Coupon.objects.filter(pk=coupon_obj.pk).update(uses_count=models.F('uses_count') + 1)
            logger.info(f"CouponUsage recorded: {coupon_code}, payment={payment_id}")

        # ── Finalize booking/purchase/event ──────────────────────────────────
        try:
            if payment_type == 'simulator':
                result = _finalize_simulator_booking(temp_id_str, payment_id)
            elif payment_type == 'package':
                result = _finalize_package_purchase(temp_id_str, payment_id)
            else:
                result = _finalize_event_registration(temp_id_str, payment_id)
        except Exception as exc:
            logger.error(
                f"CRITICAL: Square payment {payment_id} succeeded but finalization failed! "
                f"temp_id={temp_id_str}, type={payment_type}, error={exc}",
                exc_info=True
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
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
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



# ---------------------------------------------------------------------------
# Master Webhook: Square calls this for async payment events
# ---------------------------------------------------------------------------
class SquareWebhookView(APIView):
    """
    POST /api/square/webhook/

    Register this URL in the Square Developer Dashboard.
    Subscribes to: payment.completed
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw_body = request.body
        signature_header = request.headers.get('x-square-hmacsha256-signature', '')
        signature_key = getattr(settings, 'SQUARE_WEBHOOK_SIGNATURE_KEY', '').strip()

        # Skip signature check if key is not yet configured (sandbox / first-time setup)
        if signature_key:
            # Square signs the exact notification URL it sent the request to.
            # When running behind ngrok, Django sees localhost but Square used the ngrok URL.
            # Set SQUARE_WEBHOOK_URL in .env to the ngrok URL to ensure matching.
            notification_url = (
                getattr(settings, 'SQUARE_WEBHOOK_URL', '').strip()
                or request.build_absolute_uri()
            )
            if not verify_webhook_signature(raw_body, signature_header, signature_key, notification_url):
                return Response({'error': 'Invalid signature.'}, status=status.HTTP_401_UNAUTHORIZED)
        else:
            logger.warning("SQUARE_WEBHOOK_SIGNATURE_KEY not set — skipping signature verification (OK for sandbox testing).")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response({'error': 'Invalid JSON.'}, status=status.HTTP_400_BAD_REQUEST)

        event_type = payload.get('type', '')
        logger.info(f"Square webhook received: event_type={event_type}")

        if event_type != 'payment.completed':
            return Response({'message': f'Event type {event_type} not handled.'}, status=status.HTTP_200_OK)

        payment_obj = payload.get('data', {}).get('object', {}).get('payment', {})
        payment_id = payment_obj.get('id')
        payment_metadata = payment_obj.get('metadata', {})
        temp_id_str = payment_metadata.get('temp_id') or payment_obj.get('reference_id')
        payment_type = payment_metadata.get('payment_type')

        logger.info(f"Square webhook processing: payment_id={payment_id}, temp_id={temp_id_str}, type={payment_type}")

        if not temp_id_str or not payment_type:
            logger.warning(f"Square webhook missing metadata: payment_id={payment_id}")
            return Response({'message': 'Missing metadata, skipping.'}, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                if payment_type == 'simulator':
                    _finalize_simulator_booking(temp_id_str, payment_id)
                elif payment_type == 'package':
                    _finalize_package_purchase(temp_id_str, payment_id)
                elif payment_type == 'event':
                    _finalize_event_registration(temp_id_str, payment_id)
                else:
                    logger.warning(f"Square webhook: unknown payment_type={payment_type}")
        except Exception as exc:
            logger.error(f"Square webhook finalization error: {exc}", exc_info=True)

        # Always return 200 to Square
        return Response({'message': 'Webhook received.'}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Config view: Frontend fetches Square Application ID to initialize the SDK
# ---------------------------------------------------------------------------
class SquareConfigView(APIView):
    """
    GET /api/square/config/
    Returns the Square Application ID and Location ID for the frontend Web SDK.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'application_id': settings.SQUARE_APPLICATION_ID,
            'location_id': settings.SQUARE_LOCATION_ID,
            'environment': settings.SQUARE_ENVIRONMENT,
        })
