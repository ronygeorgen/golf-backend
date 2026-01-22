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

from .services import (
    sync_user_contact,
    purchase_custom_fields,
    get_first_upcoming_simulator_booking,
    get_first_upcoming_coaching_booking,
    format_booking_datetime,
    get_contact_custom_field_value,
    get_contact_custom_field_mapping,
    set_contact_custom_values,
    get_first_upcoming_special_event,
    format_special_event_datetime,
    update_user_ghl_custom_fields,
    update_ghl_cancellation_fields
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
        
        # If location_id not provided, use user's ghl_location_id from database
        if not location_id:
            location_id = getattr(user, 'ghl_location_id', None)
            if location_id:
                logger.info("Using user's ghl_location_id from database: %s", location_id)
        
        result = sync_user_contact(
            user,
            location_id=location_id,
            tags=None,
            custom_fields=custom_fields,
        )
        
        # Only log success if result is not None (sync actually succeeded)
        if result and result[0] is not None:
            logger.info("Successfully synced user %s with GHL (location: %s)", user_id, location_id or user.ghl_location_id)
        else:
            logger.warning("GHL sync returned None for user %s (location: %s). Check if location exists and OAuth is complete.", 
                         user_id, location_id or user.ghl_location_id)
        return result
    except Exception as exc:
        logger.error("Failed to sync user %s with GHL: %s", user_id, exc, exc_info=True)
        # Retry the task
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_with_ghl_task(self, purchase_id):
    """
    Async task to sync package purchase with GHL.
    """
    try:
        from coaching.models import CoachingPackagePurchase
        
        try:
            purchase = CoachingPackagePurchase.objects.select_related('client', 'package').get(id=purchase_id)
        except CoachingPackagePurchase.DoesNotExist:
            logger.error("Purchase %s not found for GHL sync", purchase_id)
            return None
        
        user = purchase.client
        location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
        
        if not location_id:
            logger.warning("No GHL location for purchase %s", purchase_id)
            return None
        
        # ✅ THIS LINE SHOULD BE ADDED - Define package_price
        package_price = purchase.package.price if purchase.package else 0
        
        # Build custom fields dict - this will create/update custom field values
        custom_fields = purchase_custom_fields(
            purchase.purchase_name or (purchase.package.title if purchase.package else 'Unknown'),
            package_price  # Now package_price is defined
        )
        
        # Sync contact using the same function as login
        result = sync_user_contact(
            user,
            location_id=location_id,
            tags=None,
            custom_fields=custom_fields,
        )
        
        logger.info("Successfully synced purchase %s with GHL", purchase_id)
        return result
    except Exception as exc:
        logger.error("Failed to sync purchase %s with GHL: %s", purchase_id, exc, exc_info=True)
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


@shared_task
def update_upcoming_booking_dates_task():
    """
    Periodic task to update upcoming booking dates in GHL custom fields.
    Runs every minute to check and update:
    - 'upcoming simulator booking date'
    - 'upcoming coaching session booking date'
    
    For each client at each onboarded location, finds the first upcoming booking
    of each type and updates the custom fields if the value has changed.
    """
    try:
        from .models import GHLLocation
        from users.models import User
        from django.utils import timezone
        
        # Get all onboarded locations
        onboarded_locations = GHLLocation.objects.filter(
            access_token__isnull=False
        ).exclude(access_token='')
        
        updated_count = 0
        skipped_count = 0
        error_count = 0
        
        for location in onboarded_locations:
            try:
                location_id = location.location_id
                
                from bookings.models import Booking
                
                # Get IDs of clients who actually have upcoming bookings or special events
                upcoming_client_ids = set(Booking.objects.filter(
                    status='confirmed',
                    start_time__gt=timezone.now()
                ).values_list('client_id', flat=True))
                
                from special_events.models import SpecialEventRegistration
                upcoming_event_user_ids = set(SpecialEventRegistration.objects.filter(
                    status='registered',
                    occurrence_date__gte=timezone.now().date()
                ).values_list('user_id', flat=True))
                
                all_active_user_ids = upcoming_client_ids.union(upcoming_event_user_ids)
                
                # Filter clients for this location who have upcoming activity
                # Include all roles (admins, staff, etc.) if they have activity
                clients = User.objects.filter(
                    id__in=all_active_user_ids,
                    ghl_location_id=location_id,
                    is_active=True,
                    phone__isnull=False
                ).exclude(phone='')
                
                logger.info(f"Processing {clients.count()} clients with upcoming bookings for location {location_id}")
                
                for client in clients:
                    try:
                        # Skip if client doesn't have a GHL contact ID
                        if not client.ghl_contact_id:
                            logger.debug(f"Client {client.id} has no GHL contact ID, skipping")
                            continue
                        
                        contact_id = client.ghl_contact_id
                        custom_fields_to_update = {}
                        
                        # Get first upcoming simulator booking
                        simulator_booking = get_first_upcoming_simulator_booking(
                            client, 
                            location_id=location_id
                        )
                        simulator_date_str = format_booking_datetime(simulator_booking) if simulator_booking else ''
                        
                        # Get current value from GHL
                        current_simulator_date = get_contact_custom_field_value(
                            contact_id,
                            location_id,
                            'upcoming_simulator_booking_date'
                        ) or ''
                        
                        # Only update if there's an actual booking (non-empty) and value is different
                        # Don't update if there's no booking - preserve existing value in GHL
                        if simulator_date_str and simulator_date_str != current_simulator_date:
                            custom_fields_to_update['upcoming simulator booking date'] = simulator_date_str
                            logger.info(
                                f"Client {client.id}: Simulator booking date changed from "
                                f"'{current_simulator_date}' to '{simulator_date_str}'"
                            )
                        
                        # Get first upcoming coaching booking
                        coaching_booking = get_first_upcoming_coaching_booking(
                            client,
                            location_id=location_id
                        )
                        coaching_date_str = format_booking_datetime(coaching_booking) if coaching_booking else ''
                        
                        # Get current value from GHL
                        current_coaching_date = get_contact_custom_field_value(
                            contact_id,
                            location_id,
                            'upcoming_coaching_session_booking_date'
                        ) or ''
                        
                        # Only update if there's an actual booking (non-empty) and value is different
                        # Don't update if there's no booking - preserve existing value in GHL
                        if coaching_date_str and coaching_date_str != current_coaching_date:
                            custom_fields_to_update['upcoming coaching session booking date'] = coaching_date_str
                            logger.info(
                                f"Client {client.id}: Coaching booking date changed from "
                                f"'{current_coaching_date}' to '{coaching_date_str}'"
                            )
                        
                        # Get first upcoming special event
                        special_event = get_first_upcoming_special_event(
                            client,
                            location_id=location_id
                        )
                        special_event_date_str = format_special_event_datetime(special_event) if special_event else ''
                        
                        # Get current value from GHL
                        current_special_event_date = get_contact_custom_field_value(
                            contact_id,
                            location_id,
                            'special_event_booked'
                        ) or ''
                        
                        # Only update if there's an actual event (non-empty) and value is different
                        if special_event_date_str and special_event_date_str != current_special_event_date:
                            custom_fields_to_update['Special Event Booked'] = special_event_date_str
                            if special_event and hasattr(special_event, 'event'):
                                name = special_event.event.title
                                if name:
                                    custom_fields_to_update['Special Event Booked Name'] = name
                            logger.info(
                                f"Client {client.id}: Special event date changed from "
                                f"'{current_special_event_date}' to '{special_event_date_str}'"
                            )
                        
                        # Update custom fields if there are changes
                        if custom_fields_to_update:
                            # Ensure field mappings exist
                            get_contact_custom_field_mapping(location_id)
                            
                            # Update the custom fields
                            success = set_contact_custom_values(
                                contact_id,
                                location_id,
                                custom_fields_to_update
                            )
                            
                            if success:
                                updated_count += 1
                                logger.info(
                                    f"Successfully updated booking dates for client {client.id} "
                                    f"(contact {contact_id})"
                                )
                            else:
                                error_count += 1
                                logger.error(
                                    f"Failed to update booking dates for client {client.id} "
                                    f"(contact {contact_id})"
                                )
                        else:
                            skipped_count += 1
                            logger.debug(
                                f"Skipped client {client.id}: no changes to booking dates"
                            )
                            
                    except Exception as exc:
                        error_count += 1
                        logger.error(
                            f"Error processing client {client.id} for location {location_id}: {exc}",
                            exc_info=True
                        )
                        continue
                
            except Exception as exc:
                logger.error(
                    f"Error processing location {location.location_id}: {exc}",
                    exc_info=True
                )
                continue
        
        logger.info(
            f"Upcoming booking dates update completed: "
            f"{updated_count} updated, {skipped_count} skipped, {error_count} errors"
        )
        
        return {
            'updated': updated_count,
            'skipped': skipped_count,
            'errors': error_count,
            'locations_processed': onboarded_locations.count()
        }
        
    except Exception as exc:
        logger.error(f"Failed to run upcoming booking dates update task: {exc}", exc_info=True)
        raise

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def update_user_ghl_custom_fields_task(self, user_id, location_id=None):
    """Async task to update user-level GHL custom fields (Total sessions, etc)"""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        update_user_ghl_custom_fields(user, location_id=location_id)
    except Exception as exc:
        logger.error(f"Failed task update_user_ghl_custom_fields for user {user_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def update_ghl_cancellation_fields_task(self, user_id, booking_id=None, registration_id=None, location_id=None):
    """Async task to track a cancellation in GHL fields"""
    try:
        from users.models import User
        user = User.objects.get(id=user_id)
        
        item = None
        if booking_id:
            from bookings.models import Booking
            item = Booking.objects.get(id=booking_id)
        elif registration_id:
            from special_events.models import SpecialEventRegistration
            item = SpecialEventRegistration.objects.get(id=registration_id)
            
        if item:
            update_ghl_cancellation_fields(user, item, location_id=location_id)
    except Exception as exc:
        logger.error(f"Failed task update_ghl_cancellation_fields for user {user_id}: {exc}")
        raise self.retry(exc=exc)
