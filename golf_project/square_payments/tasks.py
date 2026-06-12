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


@shared_task(bind=True, max_retries=2, default_retry_delay=120)
def sync_membership_hours_task(self):
    """
    Daily safety-net: reset hours for any MemberSubscription whose
    current_period_end has passed but whose hours weren't reset by webhook.
    """
    try:
        from .models import MemberSubscription
        from django.utils import timezone
        try:
            from dateutil.relativedelta import relativedelta
            HAS_DATEUTIL = True
        except ImportError:
            HAS_DATEUTIL = False

        now = timezone.now()
        overdue = MemberSubscription.objects.filter(
            status='active',
            current_period_end__lt=now,
        ).select_related('purchase', 'package', 'coaching_purchase', 'coaching_package')

        fixed = 0
        for sub in overdue:
            purchase = sub.purchase
            coaching_purchase = sub.coaching_purchase

            if purchase:
                monthly_hours = sub.package.monthly_hours
                # Only reset if hours haven't already been topped up this cycle
                if purchase.hours_remaining < monthly_hours:
                    purchase.hours_remaining = monthly_hours
                    purchase.hours_total = monthly_hours
                    purchase.package_status = 'active'
                    purchase.save(update_fields=['hours_remaining', 'hours_total', 'package_status', 'updated_at'])
            elif coaching_purchase:
                pkg = sub.coaching_package
                if coaching_purchase.sessions_remaining < pkg.monthly_sessions or coaching_purchase.simulator_hours_remaining < pkg.monthly_simulator_hours:
                    coaching_purchase.sessions_remaining = pkg.monthly_sessions
                    coaching_purchase.sessions_total = pkg.monthly_sessions
                    coaching_purchase.simulator_hours_remaining = pkg.monthly_simulator_hours
                    coaching_purchase.simulator_hours_total = pkg.monthly_simulator_hours
                    coaching_purchase.category_hours_remaining = pkg.monthly_category_hours
                    coaching_purchase.category_hours_total = pkg.monthly_category_hours
                    coaching_purchase.package_status = 'active'
                    coaching_purchase.save(update_fields=[
                        'sessions_remaining', 'sessions_total',
                        'simulator_hours_remaining', 'simulator_hours_total',
                        'category_hours_remaining', 'category_hours_total',
                        'package_status', 'updated_at'
                    ])
            else:
                continue

            # Advance the period
            if HAS_DATEUTIL:
                sub.current_period_start = sub.current_period_end
                sub.current_period_end = sub.current_period_end + relativedelta(months=1)
            else:
                sub.current_period_start = sub.current_period_end
                sub.current_period_end = sub.current_period_end + timezone.timedelta(days=30)
            sub.save(update_fields=['current_period_start', 'current_period_end', 'updated_at'])
            fixed += 1

        logger.info('sync_membership_hours_task: fixed %d overdue memberships.', fixed)

        # 2. Sweep canceled memberships whose current_period_end has passed
        expired = MemberSubscription.objects.filter(
            status='canceled',
            current_period_end__lt=now,
        ).select_related('purchase', 'coaching_purchase')

        expired_count = 0
        for sub in expired:
            updated = False
            if sub.purchase and sub.purchase.package_status != 'completed':
                sub.purchase.package_status = 'completed'
                sub.purchase.hours_remaining = 0
                sub.purchase.save(update_fields=['package_status', 'hours_remaining', 'updated_at'])
                updated = True
            if sub.coaching_purchase and sub.coaching_purchase.package_status != 'completed':
                sub.coaching_purchase.package_status = 'completed'
                sub.coaching_purchase.sessions_remaining = 0
                sub.coaching_purchase.simulator_hours_remaining = 0
                sub.coaching_purchase.category_hours_remaining = 0
                sub.coaching_purchase.save(update_fields=[
                    'package_status', 'sessions_remaining', 
                    'simulator_hours_remaining', 'category_hours_remaining', 'updated_at'
                ])
                updated = True
            if updated:
                expired_count += 1

        logger.info('sync_membership_hours_task: expired %d canceled memberships.', expired_count)
        return {'fixed': fixed, 'expired': expired_count}
    except Exception as exc:
        logger.error('sync_membership_hours_task failed: %s', exc, exc_info=True)
        raise self.retry(exc=exc)
