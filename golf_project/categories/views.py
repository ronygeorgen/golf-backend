from datetime import datetime

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.utils import get_location_id_from_request

from .models import ServiceCategory
from .serializers import ServiceCategoryAdminSerializer, ServiceCategorySerializer


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
            specific = base.filter(location_id=location_id).order_by('sort_order', 'name')
            if specific.exists():
                serializer = ServiceCategorySerializer(specific, many=True)
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

        from .availability import compute_category_slots
        slots = compute_category_slots(
            category_id=category.pk,
            booking_date=booking_date,
            location_id=location_id,
            package=package,
            coach_id=int(coach_id) if coach_id else None,
        )

        resp = {
            'date': date_str,
            'category_id': category.pk,
            'package_id': package_id,
            'coach_id': coach_id,
            'available_slots': slots,
        }
        if not slots:
            resp['message'] = 'No availability found for this date.'

        return Response(resp)
