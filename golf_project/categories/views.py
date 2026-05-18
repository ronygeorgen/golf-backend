from datetime import datetime

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.utils import get_location_id_from_request

from .models import CategoryAsset, CategoryAssetAvailability, ServiceCategory
from .serializers import (
    CategoryAssetAvailabilitySerializer,
    CategoryAssetSerializer,
    ServiceCategoryAdminSerializer,
    ServiceCategorySerializer,
)


class ActiveServiceCategoryListView(APIView):
    """
    Phase A: read-only list of categories for the booking UI.

    Resolution order for ``location_id``:
    1. Query param ``location_id`` (trimmed by caller conventions)
    2. Authenticated user's ``ghl_location_id``
    3. Fallback to default rows (location_id empty string)

    If there are active rows scoped to the resolved location, only those are returned.
    Otherwise active default rows (location_id='') are returned.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        location_id = get_location_id_from_request(request) or ''
        location_id = str(location_id).strip().rstrip('+').strip() if location_id else ''

        base = ServiceCategory.objects.filter(is_active=True)

        if location_id:
            # Return both location-specific categories AND global defaults so that
            # legacy Simulator/Coaching categories remain visible even when a location
            # has added its own custom categories (e.g. Fitness).
            qs = base.filter(
                location_id__in=[location_id, '']
            ).order_by('sort_order', 'name').distinct()
            serializer = ServiceCategorySerializer(qs, many=True)
            return Response(serializer.data)

        defaults = base.filter(location_id='').order_by('sort_order', 'name')
        serializer = ServiceCategorySerializer(defaults, many=True)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Admin CRUD ViewSet
# ---------------------------------------------------------------------------

def _is_admin_or_superadmin(user):
    role = getattr(user, 'role', '')
    return role in ('admin', 'superadmin') or getattr(user, 'is_superuser', False)


class ServiceCategoryViewSet(viewsets.ModelViewSet):
    """
    Phase B: Admin CRUD for ServiceCategory.
    - List / retrieve: filtered by location (same pattern as other admin viewsets).
    - Create / update / delete: admin or superadmin only.
    - toggle_active: PATCH shortcut to flip is_active.
    """

    serializer_class = ServiceCategoryAdminSerializer
    permission_classes = [IsAuthenticated]

    def _require_admin(self):
        if not _is_admin_or_superadmin(self.request.user):
            raise PermissionDenied("Only admins can manage service categories.")

    def get_queryset(self):
        user = self.request.user
        location_id = get_location_id_from_request(self.request) or ''

        qs = ServiceCategory.objects.all().order_by('sort_order', 'name')

        # Superadmin sees every row; admin scoped to their location + defaults
        if getattr(user, 'role', '') == 'superadmin' or getattr(user, 'is_superuser', False):
            if location_id:
                return qs.filter(location_id__in=[location_id, ''])
            return qs

        if location_id:
            return qs.filter(location_id__in=[location_id, ''])
        return qs.filter(location_id='')

    def perform_create(self, serializer):
        self._require_admin()
        location_id = get_location_id_from_request(self.request) or ''
        # Superadmins can explicitly pass location_id in the body; everyone else gets theirs
        if not (getattr(self.request.user, 'role', '') == 'superadmin' or
                getattr(self.request.user, 'is_superuser', False)):
            serializer.save(location_id=location_id)
        else:
            serializer.save()

    def perform_update(self, serializer):
        self._require_admin()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_admin()
        instance.delete()

    @action(detail=True, methods=['patch'], url_path='toggle_active')
    def toggle_active(self, request, pk=None):
        self._require_admin()
        category = self.get_object()
        category.is_active = not category.is_active
        category.save(update_fields=['is_active', 'updated_at'])
        serializer = self.get_serializer(category)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Category Assets
# ---------------------------------------------------------------------------

class CategoryAssetViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for CategoryAsset.

    Scoped to a specific ServiceCategory via the URL:
      /admin/categories/<category_pk>/assets/

    Also exposes a GET/PUT availability sub-action:
      /admin/categories/<category_pk>/assets/<pk>/availability/
    """

    serializer_class = CategoryAssetSerializer
    permission_classes = [IsAuthenticated]

    def _require_admin(self):
        if not _is_admin_or_superadmin(self.request.user):
            raise PermissionDenied("Only admins can manage category assets.")

    def get_queryset(self):
        category_pk = self.kwargs.get('category_pk') or self.request.query_params.get('category_id')
        if category_pk:
            qs = CategoryAsset.objects.filter(category_id=category_pk).order_by('sort_order', 'name')
        else:
            qs = CategoryAsset.objects.all().order_by('sort_order', 'name')
        location_id = get_location_id_from_request(self.request) or ''
        if location_id:
            qs = qs.filter(location_id__in=[location_id, ''])
        return qs

    def perform_create(self, serializer):
        self._require_admin()
        category_pk = self.kwargs.get('category_pk') or self.request.data.get('category')
        location_id = get_location_id_from_request(self.request) or ''
        if category_pk:
            serializer.save(category_id=category_pk, location_id=location_id)
        else:
            serializer.save(location_id=location_id)

    def perform_update(self, serializer):
        self._require_admin()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_admin()
        instance.delete()

    @action(detail=True, methods=['patch'], url_path='toggle_active')
    def toggle_active(self, request, pk=None):
        self._require_admin()
        asset = self.get_object()
        asset.is_active = not asset.is_active
        asset.save(update_fields=['is_active', 'updated_at'])
        return Response(CategoryAssetSerializer(asset).data)

    @action(detail=True, methods=['get', 'put'], url_path='availability')
    def availability(self, request, pk=None, category_pk=None):
        """
        GET  → returns the weekly availability schedule for this asset.
        PUT  → replaces the schedule (list of {day_of_week, start_time, end_time}).
               Omit an entry to delete it; include {id, deleted: true} to delete by id.
        """
        asset = self.get_object()
        self._require_admin()

        if request.method == 'GET':
            avails = CategoryAssetAvailability.objects.filter(asset=asset).order_by('day_of_week', 'start_time')
            return Response(CategoryAssetAvailabilitySerializer(avails, many=True).data)

        # PUT — replace schedule
        data = request.data
        if not isinstance(data, list):
            return Response({'error': 'Availability data must be a list.'}, status=status.HTTP_400_BAD_REQUEST)

        from datetime import datetime as dt
        updated = []
        for item in data:
            if item.get('deleted') and item.get('id'):
                CategoryAssetAvailability.objects.filter(id=item['id'], asset=asset).delete()
                continue
            dow = item.get('day_of_week')
            if dow is None:
                continue
            try:
                s = dt.strptime(item.get('start_time', '09:00'), '%H:%M').time()
                e = dt.strptime(item.get('end_time', '17:00'), '%H:%M').time()
                avail, _ = CategoryAssetAvailability.objects.update_or_create(
                    asset=asset,
                    day_of_week=int(dow),
                    start_time=s,
                    defaults={'end_time': e},
                )
                updated.append(avail)
            except (ValueError, TypeError):
                pass

        all_avail = CategoryAssetAvailability.objects.filter(asset=asset).order_by('day_of_week', 'start_time')
        return Response(CategoryAssetAvailabilitySerializer(all_avail, many=True).data)


# ---------------------------------------------------------------------------
# Phase E: Slot availability for non-legacy categories
# ---------------------------------------------------------------------------


class CategorySlotsView(APIView):
    """
    GET /api/categories/<pk>/slots/?date=YYYY-MM-DD[&package_id=<id>][&coach_id=<id>]

    Returns available time slots for a non-legacy service category
    (legacy_booking_type=None).  Staff eligibility is determined by
    StaffCategory assignments rather than CoachingPackage.staff_members.

    If package_id is given, the package's session_duration_minutes is used
    and (if the package has explicit staff) the staff list is intersected.

    The response shape is intentionally identical to check_coaching_availability
    so the frontend can share rendering code:
        {
            "date": "2024-03-15",
            "category_id": 7,
            "package_id": 3,       # echoed or null
            "coach_id": null,
            "available_slots": [...],
            "message": "..."        # only on empty result
        }
    """

    permission_classes = [AllowAny]

    def get(self, request, pk=None):
        # Resolve category
        try:
            category = ServiceCategory.objects.get(pk=pk, is_active=True)
        except ServiceCategory.DoesNotExist:
            return Response({'error': 'Category not found.'}, status=status.HTTP_404_NOT_FOUND)

        date_str = request.query_params.get('date')
        if not date_str:
            return Response({'error': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format, use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve location_id the same way as the rest of the API
        location_id = get_location_id_from_request(request) or ''
        location_id = str(location_id).strip().rstrip('+').strip() if location_id else ''
        if not location_id:
            # Fall back to category's own location_id (for default categories)
            location_id = category.location_id or ''

        # Optional package filter
        package = None
        package_id = request.query_params.get('package_id')
        if package_id:
            from coaching.models import CoachingPackage
            try:
                package = CoachingPackage.objects.get(
                    pk=package_id,
                    service_category=category,
                    is_active=True,
                )
            except CoachingPackage.DoesNotExist:
                return Response(
                    {'error': 'Package not found for this category.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Optional single-coach filter
        coach_id = request.query_params.get('coach_id')

        # Optional asset filter (drives staff-based vs. asset-based slot logic)
        asset_id = request.query_params.get('asset_id')
        asset_id = int(asset_id) if asset_id and str(asset_id).isdigit() else None

        # Optional duration override (used for asset-only bookings, mirrors simulator behaviour)
        duration_raw = request.query_params.get('duration')
        duration_minutes = int(duration_raw) if duration_raw and str(duration_raw).isdigit() else None

        from .availability import compute_category_slots
        slots = compute_category_slots(
            category_id=category.pk,
            booking_date=booking_date,
            location_id=location_id,
            package=package,
            coach_id=int(coach_id) if coach_id else None,
            asset_id=asset_id,
            duration_minutes=duration_minutes,
        )

        resp = {
            'date': date_str,
            'category_id': category.pk,
            'package_id': package_id,
            'coach_id': coach_id,
            'asset_id': asset_id,
            'available_slots': slots,
        }
        if not slots:
            resp['message'] = 'No availability found for this date.'

        return Response(resp)
