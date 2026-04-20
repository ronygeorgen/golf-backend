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
