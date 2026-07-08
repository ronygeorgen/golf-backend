from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied
import logging

logger = logging.getLogger(__name__)

class IsActiveLocationMember(permissions.BasePermission):
    """
    Global permission check to ensure the authenticated user belongs to an active location.
    - Superadmins are exempt.
    - Unauthenticated users are ignored (handled by IsAuthenticated/AllowAny).
    - If the user's location is inactive, raises PermissionDenied.
    """
    def has_permission(self, request, view):
        # Ignore unauthenticated users
        if not request.user or not request.user.is_authenticated:
            return True

        # Superadmins bypass this check
        if getattr(request.user, 'role', '') == 'superadmin':
            return True

        # Check location status
        location_id = getattr(request.user, 'ghl_location_id', None)
        if location_id:
            from ghl.models import GHLLocation
            loc = GHLLocation.objects.filter(location_id=location_id).only('status').first()
            if loc and loc.status != 'active':
                logger.warning("Blocked access for user %s to %s because location %s is inactive.", request.user.id, request.path, location_id)
                raise PermissionDenied("Your location is currently inactive. Please contact support.")

        return True
