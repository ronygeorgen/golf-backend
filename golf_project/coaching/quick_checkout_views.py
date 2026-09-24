"""
Quick Checkout APIs for staff/admin:
- Create one-off packages (not listed online) matching UnifiedPackagesPage rules
- Create temp purchase for catalog or one-off packages

Combo rules (same as package admin):
  - legacy coaching  → sessions + optional simulator_hours
  - dynamic category → sessions + optional category_hours (asset time)
  - legacy simulator → hours only (SimulatorPackage)
"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.utils import get_location_id_from_request

from .models import CoachingPackage, SimulatorPackage, TempPurchase

logger = logging.getLogger(__name__)


def _staff_only(user):
    return (
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, 'role', None) in ['admin', 'staff', 'superadmin'])
    )


class QuickCheckoutOneOffView(APIView):
    """
    POST /api/coaching/quick-checkout/one-off/

    Body (preferred — matches package admin):
      {
        "buyer_phone": "...",
        "service_category_id": 3,          # required
        "title": "Special — 5 Football sessions",
        "price": "150.00",
        "session_count": 5,                # coaching / dynamic
        "session_duration_minutes": 60,
        "simulator_hours": 2.0,            # coaching combo only
        "category_hours": 2.0,             # dynamic category combo only
        "referral_id": optional
      }

    Legacy package_kind still accepted for older clients:
      coaching | simulator | combo | category
      — normalized against service_category.legacy_booking_type when provided.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not _staff_only(request.user):
            return Response({'error': 'Staff or admin only.'}, status=status.HTTP_403_FORBIDDEN)

        buyer_phone = (request.data.get('buyer_phone') or '').strip()
        title = (request.data.get('title') or '').strip()
        if not title:
            return Response({'error': 'title is required.'}, status=status.HTTP_400_BAD_REQUEST)
        location_id = get_location_id_from_request(request) or getattr(request.user, 'ghl_location_id', None)
        package_kind = (request.data.get('package_kind') or '').strip().lower()

        if not buyer_phone:
            return Response({'error': 'buyer_phone is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            price = Decimal(str(request.data.get('price', '0')))
        except (InvalidOperation, TypeError):
            return Response({'error': 'Invalid price.'}, status=status.HTTP_400_BAD_REQUEST)
        if price <= 0:
            return Response({'error': 'price must be greater than 0.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session_count = int(request.data.get('session_count') or 0)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid session_count.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            simulator_hours = Decimal(str(request.data.get('simulator_hours') or 0))
        except (InvalidOperation, TypeError):
            return Response({'error': 'Invalid simulator_hours.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            category_hours = Decimal(str(request.data.get('category_hours') or 0))
        except (InvalidOperation, TypeError):
            return Response({'error': 'Invalid category_hours.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            session_duration = int(request.data.get('session_duration_minutes') or 60)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid session_duration_minutes.'}, status=status.HTTP_400_BAD_REQUEST)

        from categories.models import ServiceCategory

        raw_cat_id = request.data.get('service_category_id')
        service_category = None
        if raw_cat_id not in (None, '', 0, '0'):
            try:
                service_category = ServiceCategory.objects.get(id=int(raw_cat_id), is_active=True)
            except (ServiceCategory.DoesNotExist, TypeError, ValueError):
                return Response({'error': 'Invalid service_category_id.'}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve mode from category (same rules as UnifiedPackagesPage), with package_kind fallback
        legacy = getattr(service_category, 'legacy_booking_type', None) if service_category else None
        if service_category:
            if legacy == 'simulator':
                mode = 'simulator'
            elif legacy == 'coaching':
                mode = 'coaching'  # sessions + optional sim hours
            else:
                mode = 'dynamic'  # sessions + optional category hours
        elif package_kind == 'simulator':
            mode = 'simulator'
        elif package_kind in ('coaching', 'combo'):
            # combo without category → legacy coaching+sim combo
            mode = 'coaching'
            if package_kind == 'combo' and simulator_hours <= 0 and session_count < 1:
                return Response(
                    {'error': 'Coaching combo needs sessions and/or simulator hours.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif package_kind == 'category':
            return Response(
                {'error': 'service_category_id is required for category packages.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            return Response(
                {'error': 'service_category_id is required (or package_kind=simulator|coaching|combo).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Enforce product combo rules — do not mix sim + category hours
        if mode == 'simulator':
            hours = simulator_hours if simulator_hours > 0 else category_hours
            if hours <= 0:
                return Response({'error': 'simulator hours are required.'}, status=status.HTTP_400_BAD_REQUEST)
            simulator_hours = hours
            session_count = 0
            category_hours = Decimal('0')
        elif mode == 'coaching':
            if session_count < 1:
                return Response({'error': 'session_count is required for coaching packages.'}, status=status.HTTP_400_BAD_REQUEST)
            category_hours = Decimal('0')  # coaching combo uses simulator_hours only
        else:  # dynamic
            if session_count < 1:
                return Response(
                    {'error': 'session_count is required (same as package admin for this category).'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            simulator_hours = Decimal('0')  # dynamic combo uses category_hours only
            if category_hours < 0:
                return Response({'error': 'category_hours cannot be negative.'}, status=status.HTTP_400_BAD_REQUEST)

        referral_user = request.user
        referral_id = request.data.get('referral_id')
        if referral_id:
            from users.models import User
            try:
                referral_user = User.objects.get(id=referral_id, role__in=['superadmin', 'admin', 'staff'])
            except User.DoesNotExist:
                return Response({'error': 'Invalid referral_id.'}, status=status.HTTP_400_BAD_REQUEST)

        description = request.data.get('description') or 'Staff Quick Checkout one-off package (not listed online).'

        if mode == 'simulator':
            package = SimulatorPackage.objects.create(
                title=title,
                description=description,
                price=price,
                hours=simulator_hours,
                location_id=location_id,
                is_active=True,
                is_one_off=True,
            )
            package_type = 'simulator'
            temp = TempPurchase(
                simulator_package=package,
                buyer_phone=buyer_phone,
                purchase_type='normal',
                package_type='simulator',
                recipients=[],
                referral_id=referral_user,
            )
            out_kind = 'simulator'
            out_sim = simulator_hours
            out_cat_hrs = Decimal('0')
        else:
            is_combo = (mode == 'coaching' and simulator_hours > 0) or (mode == 'dynamic' and category_hours > 0)
            package = CoachingPackage.objects.create(
                title=title,
                description=description,
                price=price,
                session_count=session_count,
                session_duration_minutes=session_duration,
                simulator_hours=simulator_hours if mode == 'coaching' else Decimal('0'),
                category_hours=category_hours if mode == 'dynamic' else Decimal('0'),
                service_category=service_category,
                location_id=location_id,
                is_active=True,
                is_one_off=True,
            )
            package_type = 'coaching'
            temp = TempPurchase(
                package=package,
                buyer_phone=buyer_phone,
                purchase_type='normal',
                package_type='coaching',
                recipients=[],
                referral_id=referral_user,
            )
            out_kind = 'combo' if is_combo else ('coaching' if mode == 'coaching' else 'category')
            out_sim = package.simulator_hours
            out_cat_hrs = package.category_hours

        temp.save()

        return Response(
            {
                'temp_id': str(temp.temp_id),
                'package_id': package.id,
                'package_type': package_type,
                'package_kind': out_kind,
                'mode': mode,
                'title': package.title,
                'price': str(package.price),
                'service_category_id': service_category.id if service_category else None,
                'session_count': getattr(package, 'session_count', None) or 0,
                'simulator_hours': str(out_sim),
                'category_hours': str(out_cat_hrs),
                'is_one_off': True,
                'is_combo': out_kind == 'combo',
                'message': 'One-off package created. Proceed to payment.',
            },
            status=status.HTTP_201_CREATED,
        )


class QuickCheckoutCatalogTempView(APIView):
    """
    POST /api/coaching/quick-checkout/temp-purchase/
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        if not _staff_only(request.user):
            return Response({'error': 'Staff or admin only.'}, status=status.HTTP_403_FORBIDDEN)

        package_id = request.data.get('package_id')
        buyer_phone = (request.data.get('buyer_phone') or '').strip()
        package_type = request.data.get('package_type')

        if not package_id or not buyer_phone or package_type not in ('coaching', 'simulator'):
            return Response(
                {'error': 'package_id, buyer_phone, and package_type (coaching|simulator) are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        referral_user = request.user
        referral_id = request.data.get('referral_id')
        if referral_id:
            from users.models import User
            try:
                referral_user = User.objects.get(id=referral_id, role__in=['superadmin', 'admin', 'staff'])
            except User.DoesNotExist:
                return Response({'error': 'Invalid referral_id.'}, status=status.HTTP_400_BAD_REQUEST)

        if package_type == 'simulator':
            try:
                package = SimulatorPackage.objects.get(id=package_id, is_active=True)
            except SimulatorPackage.DoesNotExist:
                return Response({'error': 'Simulator package not found or inactive.'}, status=status.HTTP_404_NOT_FOUND)
            temp = TempPurchase(
                simulator_package=package,
                buyer_phone=buyer_phone,
                purchase_type='normal',
                package_type='simulator',
                recipients=[],
                referral_id=referral_user,
            )
            price = package.price
            title = package.title
        else:
            try:
                package = CoachingPackage.objects.get(id=package_id, is_active=True)
            except CoachingPackage.DoesNotExist:
                return Response({'error': 'Coaching package not found or inactive.'}, status=status.HTTP_404_NOT_FOUND)
            temp = TempPurchase(
                package=package,
                buyer_phone=buyer_phone,
                purchase_type='normal',
                package_type='coaching',
                recipients=[],
                referral_id=referral_user,
            )
            price = package.price
            title = package.title

        temp.save()
        return Response(
            {
                'temp_id': str(temp.temp_id),
                'package_id': package.id,
                'package_type': package_type,
                'title': title,
                'price': str(price),
                'is_one_off': bool(getattr(package, 'is_one_off', False)),
                'message': 'Temp purchase created. Proceed to payment.',
            },
            status=status.HTTP_201_CREATED,
        )
