import logging
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser

from .models import Coupon, CouponUsage
from .serializers import CouponSerializer, CouponUsageSerializer, CouponValidateSerializer

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Full CRUD for coupons
# ─────────────────────────────────────────────────────────────────────────────

class CouponListCreateView(APIView):
    """GET all coupons / POST create a new coupon (admin only)."""
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        coupons = Coupon.objects.all()
        serializer = CouponSerializer(coupons, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = CouponSerializer(data=request.data)
        if serializer.is_valid():
            coupon = serializer.save()
            return Response(CouponSerializer(coupon).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CouponDetailView(APIView):
    """GET / PUT / DELETE a single coupon (admin only)."""
    permission_classes = [IsAuthenticated, IsAdminUser]

    def _get_coupon(self, pk):
        try:
            return Coupon.objects.get(pk=pk)
        except Coupon.DoesNotExist:
            return None

    def get(self, request, pk):
        coupon = self._get_coupon(pk)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = CouponSerializer(coupon)
        usages = CouponUsage.objects.filter(coupon=coupon).order_by('-used_at')[:50]
        return Response({
            **serializer.data,
            'recent_usages': CouponUsageSerializer(usages, many=True).data,
        })

    def put(self, request, pk):
        coupon = self._get_coupon(pk)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = CouponSerializer(coupon, data=request.data, partial=True)
        if serializer.is_valid():
            coupon = serializer.save()
            return Response(CouponSerializer(coupon).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        coupon = self._get_coupon(pk)
        if not coupon:
            return Response({'error': 'Coupon not found.'}, status=status.HTTP_404_NOT_FOUND)
        coupon.delete()
        return Response({'message': 'Coupon deleted.'}, status=status.HTTP_204_NO_CONTENT)


class CouponUsageListView(APIView):
    """GET all coupon usage records (admin only)."""
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        usages = CouponUsage.objects.select_related('coupon', 'user').all()
        
        # Apply filters from query params
        user_query = request.query_params.get('user')
        if user_query:
            from django.db.models import Q
            usages = usages.filter(
                Q(user__first_name__icontains=user_query) |
                Q(user__last_name__icontains=user_query) |
                Q(customer_email__icontains=user_query) |
                Q(customer_phone__icontains=user_query)
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
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CouponValidateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.validated_data['code'].upper().strip()
        amount = float(serializer.validated_data['amount'])
        payment_type = serializer.validated_data.get('payment_type')

        # Resolve identity from authenticated user
        user = request.user
        email = getattr(user, 'email', None)
        phone = getattr(user, 'phone', None)

        try:
            coupon = Coupon.objects.get(code=code)
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
