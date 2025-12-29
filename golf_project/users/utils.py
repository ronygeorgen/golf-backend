"""
Utility functions for location-based filtering
"""
from django.db.models import Q
from .models import User


def get_location_id_from_request(request):
    """
    Extract location_id from request.
    Priority:
    1. location_id from request body (POST/PUT/PATCH) - only if body is a dict
    2. location_id from query params (GET)
    3. User's ghl_location_id (if authenticated)
    4. None
    """
    location_id = None
    
    # Try to get from request body first (only if it's a dict, not a list)
    if hasattr(request, 'data') and request.data:
        # Check if request.data is a dict (not a list)
        if isinstance(request.data, dict):
            location_id = request.data.get('location_id')
        # If it's a list, we can't extract location_id from it, so skip to query params
    
    # If not in body, try query params
    if not location_id and hasattr(request, 'query_params'):
        location_id = request.query_params.get('location_id')
    
    # If still not found and user is authenticated, use user's location_id
    if not location_id and hasattr(request, 'user') and request.user.is_authenticated:
        location_id = getattr(request.user, 'ghl_location_id', None)
    
    return location_id


def filter_by_location(queryset, location_id, location_field='location_id'):
    """
    Filter a queryset by location_id.
    
    Args:
        queryset: Django QuerySet to filter
        location_id: Location ID to filter by (can be None)
        location_field: Name of the field containing location_id (default: 'location_id')
    
    Returns:
        Filtered QuerySet
    """
    if location_id:
        filter_kwargs = {location_field: location_id}
        return queryset.filter(**filter_kwargs)
    return queryset


def get_users_by_location(location_id):
    """
    Get all users (clients, staff, admin) for a specific location.
    """
    if location_id:
        return User.objects.filter(ghl_location_id=location_id)
    return User.objects.none()

