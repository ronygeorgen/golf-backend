"""
Celery tasks for GHL integration.
"""
import logging

try:
    from celery import shared_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    # Fallback decorator that does nothing if Celery is not available
    def shared_task(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

from django.conf import settings

from .services import (
    sync_user_contact,
    build_purchase_tags,
    purchase_custom_fields,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_user_contact_task(self, user_id, location_id=None, tags=None, custom_fields=None):
    """
    Async task to sync user contact with GHL.
    
    Args:
        user_id: ID of the user to sync
        location_id: Optional GHL location ID
        tags: Optional list of tags to add
        custom_fields: Optional dict of custom fields
    """
    try:
        from users.models import User
        
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            logger.error("User %s not found for GHL sync", user_id)
            return None
        
        result = sync_user_contact(
            user,
            location_id=location_id,
            tags=tags,
            custom_fields=custom_fields,
        )
        
        logger.info("Successfully synced user %s with GHL", user_id)
        return result
    except Exception as exc:
        logger.error("Failed to sync user %s with GHL: %s", user_id, exc, exc_info=True)
        # Retry the task
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_with_ghl_task(self, purchase_id):
    """
    Async task to sync package purchase with GHL.
    
    Args:
        purchase_id: ID of the CoachingPackagePurchase to sync
    """
    try:
        from coaching.models import CoachingPackagePurchase
        
        try:
            purchase = CoachingPackagePurchase.objects.select_related('client', 'package').get(id=purchase_id)
        except CoachingPackagePurchase.DoesNotExist:
            logger.error("Purchase %s not found for GHL sync", purchase_id)
            return None
        
        user = purchase.client
        location_id = getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
        
        if not location_id:
            logger.warning("No GHL location for purchase %s", purchase_id)
            return None
        
        # Build tags and custom fields
        package_price = purchase.package.price if purchase.package else 0
        tags = build_purchase_tags(package_price)
        custom_fields = purchase_custom_fields(
            purchase.purchase_name or purchase.package.title if purchase.package else 'Unknown',
            package_price
        )
        
        result = sync_user_contact(
            user,
            location_id=location_id,
            tags=tags,
            custom_fields=custom_fields,
        )
        
        logger.info("Successfully synced purchase %s with GHL", purchase_id)
        return result
    except Exception as exc:
        logger.error("Failed to sync purchase %s with GHL: %s", purchase_id, exc, exc_info=True)
        # Retry the task
        raise self.retry(exc=exc)


@shared_task
def refresh_ghl_tokens_task():
    """
    Periodic task to refresh OAuth tokens for all active GHL locations.
    This task should run periodically (e.g., every hour) via Celery Beat.
    """
    try:
        from .models import GHLLocation
        from .services import GHLClient
        from django.utils import timezone
        
        # Get all active locations that need token refresh
        active_locations = GHLLocation.objects.filter(
            status='active',
            refresh_token__isnull=False
        ).exclude(refresh_token='')
        
        refreshed_count = 0
        failed_count = 0
        
        for location in active_locations:
            try:
                # Check if token needs refresh (within 5 minutes of expiry)
                if location.needs_token_refresh():
                    logger.info("Refreshing token for location %s", location.location_id)
                    client = GHLClient(location_id=location.location_id)
                    # This will automatically refresh the token
                    client._get_location()
                    refreshed_count += 1
                    logger.info("Successfully refreshed token for location %s", location.location_id)
                else:
                    logger.debug("Token for location %s is still valid, skipping refresh", location.location_id)
            except Exception as exc:
                logger.error("Failed to refresh token for location %s: %s", location.location_id, exc, exc_info=True)
                failed_count += 1
        
        logger.info("Token refresh completed: %d refreshed, %d failed, %d skipped", 
                   refreshed_count, failed_count, active_locations.count() - refreshed_count - failed_count)
        
        return {
            'refreshed': refreshed_count,
            'failed': failed_count,
            'total_checked': active_locations.count()
        }
    except Exception as exc:
        logger.error("Failed to run token refresh task: %s", exc, exc_info=True)
        raise

