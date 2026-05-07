"""
Celery tasks for Square payment OAuth token management.
"""
import logging

try:
    from celery import shared_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    def shared_task(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=300)
def refresh_square_tokens_task(self):
    """
    Periodic Celery Beat task — runs every hour.

    Iterates all connected LocationSquareAccount records and refreshes the
    access_token for any account whose token expires within 30 minutes.

    Mirrors the pattern used by ghl.tasks.refresh_ghl_tokens_task.
    """
    try:
        from .models import LocationSquareAccount
        from .services import refresh_oauth_token
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime
        import datetime

        accounts = LocationSquareAccount.objects.filter(
            is_connected=True,
            refresh_token__isnull=False,
        ).exclude(refresh_token='')

        refreshed = 0
        skipped = 0
        failed = 0

        for account in accounts:
            try:
                if not account.needs_token_refresh():
                    skipped += 1
                    logger.debug(
                        "Square token for location %s is still valid, skipping.",
                        account.ghl_location.location_id,
                    )
                    continue

                logger.info(
                    "Refreshing Square token for location %s (merchant=%s)",
                    account.ghl_location.location_id, account.merchant_id,
                )

                data = refresh_oauth_token(account.refresh_token)

                # Parse expires_at (ISO 8601 string from Square)
                expires_at_str = data.get('expires_at', '')
                token_expires_at = None
                if expires_at_str:
                    try:
                        token_expires_at = parse_datetime(expires_at_str)
                        if token_expires_at and not token_expires_at.tzinfo:
                            token_expires_at = token_expires_at.replace(tzinfo=datetime.timezone.utc)
                    except Exception:
                        pass
                if not token_expires_at:
                    token_expires_at = timezone.now() + timezone.timedelta(days=30)

                account.access_token = data['access_token']
                account.token_expires_at = token_expires_at
                # refresh_token may or may not be returned on refresh
                if data.get('refresh_token'):
                    account.refresh_token = data['refresh_token']
                account.save(update_fields=['access_token', 'refresh_token', 'token_expires_at', 'updated_at'])

                refreshed += 1
                logger.info(
                    "Successfully refreshed Square token for location %s",
                    account.ghl_location.location_id,
                )

            except Exception as exc:
                failed += 1
                logger.error(
                    "Failed to refresh Square token for location %s: %s",
                    account.ghl_location.location_id, exc,
                    exc_info=True,
                )

        logger.info(
            "Square token refresh complete: %d refreshed, %d skipped, %d failed.",
            refreshed, skipped, failed,
        )
        return {'refreshed': refreshed, 'skipped': skipped, 'failed': failed}

    except Exception as exc:
        logger.error("Square token refresh task failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def refresh_single_square_token_task(self, ghl_location_id: str):
    """
    Refresh the Square token for a single GHL location on demand.

    Args:
        ghl_location_id: The GHLLocation.location_id string.
    """
    try:
        from .models import LocationSquareAccount
        from .services import refresh_oauth_token
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime
        import datetime

        account = LocationSquareAccount.objects.select_related('ghl_location').get(
            ghl_location__location_id=ghl_location_id,
            is_connected=True,
        )

        data = refresh_oauth_token(account.refresh_token)

        expires_at_str = data.get('expires_at', '')
        token_expires_at = None
        if expires_at_str:
            try:
                token_expires_at = parse_datetime(expires_at_str)
                if token_expires_at and not token_expires_at.tzinfo:
                    token_expires_at = token_expires_at.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                pass
        if not token_expires_at:
            token_expires_at = timezone.now() + timezone.timedelta(days=30)

        account.access_token = data['access_token']
        account.token_expires_at = token_expires_at
        if data.get('refresh_token'):
            account.refresh_token = data['refresh_token']
        account.save(update_fields=['access_token', 'refresh_token', 'token_expires_at', 'updated_at'])

        logger.info("Square token refreshed for location %s", ghl_location_id)
        return {'status': 'refreshed', 'ghl_location_id': ghl_location_id}

    except LocationSquareAccount.DoesNotExist:
        logger.error("No connected Square account for location %s", ghl_location_id)
        return {'status': 'not_found'}
    except Exception as exc:
        logger.error("Failed to refresh Square token for location %s: %s", ghl_location_id, exc, exc_info=True)
        raise self.retry(exc=exc)
