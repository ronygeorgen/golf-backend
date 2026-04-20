from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from users.utils import get_location_id_from_request

from .models import ServiceCategory
from .serializers import ServiceCategorySerializer


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
