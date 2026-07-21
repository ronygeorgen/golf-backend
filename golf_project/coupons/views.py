import logging
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from users.permissions import IsActiveLocationMember

from .models import Coupon, CouponUsage
from .serializers import CouponSerializer, CouponUsageSerializer, CouponValidateSerializer

logger = logging.getLogger(__name__)


class IsAdminOrSuperAdmin(BasePermission):
    """Allow access to users with role 'admin' or 'superadmin' only."""

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and getattr(request.user, 'role', None) in ('admin', 'superadmin')
        )


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Full CRUD for coupons (location-scoped)
# ─────────────────────────────────────────────────────────────────────────────

class CouponListCreateView(APIView):
    """GET all coupons / POST create a new coupon (admin only)."""
    permission_classes = [IsAuthenticated, IsActiveLocationMember, IsAdminOrSuperAdmin]

    def get(self, request):
        from users.utils import get_location_id_from_request
        from django.db.models import Q
        location_id = get_location_id_from_request(request)
        is_superadmin = (
            getattr(request.user, 'is_superuser', False)
            or getattr(request.user, 'role', '') == 'superadmin'
        )

        coupons = Coupon.objects.all()
        if not is_superadmin and location_id:
            # Show coupons for this location OR global coupons (location_id is NULL/empty)
            coupons = coupons.filter(
                Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
            )
        serializer = CouponSerializer(coupons, many=True)
        return Response(serializer.data)

    def post(self, request):
        from users.utils import get_location_id_from_request
        location_id = get_location_id_from_request(request)
        serializer = CouponSerializer(data=request.data)
        if serializer.is_valid():
            # Attach the admin's location_id when creating the coupon
            coupon = serializer.save(location_id=location_id if location_id else None)
            return Response(CouponSerializer(coupon).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CouponDetailView(APIView):
    """GET / PUT / DELETE a single coupon (admin only)."""
    permission_classes = [IsAuthenticated, IsActiveLocationMember, IsAdminOrSuperAdmin]

    def _get_coupon(self, pk, request):
        from users.utils import get_location_id_from_request
        try:
            coupon = Coupon.objects.get(pk=pk)
        except Coupon.DoesNotExist:
            return None
        # Non-superadmins can only manage coupons that belong to their location (or global ones)
        is_superadmin = (
            getattr(request.user, 'is_superuser', False)
            or getattr(request.user, 'role', '') == 'superadmin'
        )
        if not is_superadmin:
            location_id = get_location_id_from_request(request)
            if location_id and coupon.location_id and coupon.location_id != location_id:
                return None  # Not found / not accessible
        return coupon

    def get(self, request, pk):
        coupon = self._get_coupon(pk, request)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = CouponSerializer(coupon)
        usages = CouponUsage.objects.filter(coupon=coupon).order_by('-used_at')[:50]
        return Response({
            **serializer.data,
            'recent_usages': CouponUsageSerializer(usages, many=True).data,
        })

    def put(self, request, pk):
        coupon = self._get_coupon(pk, request)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = CouponSerializer(coupon, data=request.data, partial=True)
        if serializer.is_valid():
            coupon = serializer.save()
            return Response(CouponSerializer(coupon).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        coupon = self._get_coupon(pk, request)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        coupon.delete()
        return Response({'message': 'Coupon deleted.'}, status=status.HTTP_204_NO_CONTENT)


class CouponUsageListView(APIView):
    """GET all coupon usage records (admin only)."""
    permission_classes = [IsAuthenticated, IsActiveLocationMember, IsAdminOrSuperAdmin]

    def get(self, request):
        from users.utils import get_location_id_from_request
        from django.db.models import Q
        location_id = get_location_id_from_request(request)
        is_superadmin = (
            getattr(request.user, 'is_superuser', False)
            or getattr(request.user, 'role', '') == 'superadmin'
        )

        usages = CouponUsage.objects.select_related('coupon', 'user').all()

        # Scope to this location's coupons (or global coupons) for non-superadmins
        if not is_superadmin and location_id:
            usages = usages.filter(
                Q(coupon__location_id=location_id)
                | Q(coupon__location_id__isnull=True)
                | Q(coupon__location_id='')
            )

        # Apply filters from query params
        user_query = request.query_params.get('user')
        if user_query:
            usages = usages.filter(
                Q(user__first_name__icontains=user_query)
                | Q(user__last_name__icontains=user_query)
                | Q(customer_email__icontains=user_query)
                | Q(customer_phone__icontains=user_query)
            )

        coupon_query = request.query_params.get('coupon')
        if coupon_query:
            usages = usages.filter(coupon__code__icontains=coupon_query)

        purpose = request.query_params.get('purpose')
        if purpose:
            # Handles both exact matches and prefix matches (e.g. 'asset' matches 'asset:3')
            usages = usages.filter(payment_type__icontains=purpose)

        start_date = request.query_params.get('start_date')
        if start_date:
            usages = usages.filter(used_at__date__gte=start_date)

        end_date = request.query_params.get('end_date')
        if end_date:
            usages = usages.filter(used_at__date__lte=end_date)

        label = request.query_params.get('label')
        if label:
            usages = usages.filter(item_label__icontains=label)

        usages = usages.order_by('-used_at')
        serializer = CouponUsageSerializer(usages, many=True)
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# Public: Validate a coupon (authenticated users)
# ─────────────────────────────────────────────────────────────────────────────

class CouponValidateView(APIView):
    """POST validate a coupon code and return discount info. Does NOT consume the coupon."""
    permission_classes = [IsAuthenticated, IsActiveLocationMember]

    def post(self, request):
        from users.utils import get_location_id_from_request
        from django.db.models import Q
        serializer = CouponValidateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data['code'].upper().strip()
        amount = float(serializer.validated_data['amount'])
        payment_type = serializer.validated_data.get('payment_type')
        package_id = serializer.validated_data.get('package_id')
        event_id = serializer.validated_data.get('event_id')

        # If a specific package_id is provided and payment_type is 'package',
        # construct the specific token so per-package coupon restrictions work.
        if payment_type == 'package' and package_id:
            payment_type = f'package:{package_id}'

        # If a specific event_id is provided and payment_type is 'event',
        # construct the specific token so per-event coupon restrictions work.
        if payment_type == 'event' and event_id:
            payment_type = f'event:{event_id}'

        # Resolve identity from authenticated user
        user = request.user
        email = getattr(user, 'email', None)
        phone = getattr(user, 'phone', None)
        location_id = get_location_id_from_request(request)

        # Find the coupon — restrict to location-specific or global coupons
        coupon_qs = Coupon.objects.filter(code=code)
        if location_id:
            coupon_qs = coupon_qs.filter(
                Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
            )

        try:
            coupon = coupon_qs.get()
        except Coupon.DoesNotExist:
            return Response({'error': 'Invalid coupon code.'}, status=status.HTTP_404_NOT_FOUND)

        # Check validity (payment_type + per-user limit)
        valid, error_msg = coupon.is_valid(
            payment_type=payment_type,
            user=user,
            email=email,
            phone=phone
        )
        if not valid:
            return Response({'error': error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Calculate discount
        discount_amount = coupon.calculate_discount(amount)
        final_amount = round(amount - discount_amount, 2)

        return Response({
            'valid': True,
            'coupon_id': coupon.id,
            'code': coupon.code,
            'discount_type': coupon.discount_type,
            'discount_value': float(coupon.discount_value),
            'discount_amount': discount_amount,
            'original_amount': amount,
            'final_amount': final_amount,
            'description': coupon.description,
        })
