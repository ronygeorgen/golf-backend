"""
Management command to clean up expired temporary bookings.

This command should be run periodically (e.g., via cron or Celery beat) to:
1. Mark expired TempBookings as 'expired'
2. Delete old completed/expired TempBookings to prevent database bloat

Usage:
    python manage.py cleanup_temp_bookings
    python manage.py cleanup_temp_bookings --delete-old --days=7
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from bookings.models import TempBooking
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up expired temporary bookings'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete-old',
            action='store_true',
            help='Delete old completed/expired temp bookings',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days to keep completed/expired bookings (default: 7)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        delete_old = options['delete_old']
        days_to_keep = options['days']
        
        now = timezone.now()
        
        # Step 1: Mark expired TempBookings
        self.stdout.write(self.style.WARNING('\n=== Marking Expired TempBookings ==='))
        
        expired_query = TempBooking.objects.filter(
            status='reserved',
            expires_at__lte=now
        )
        
        expired_count = expired_query.count()
        self.stdout.write(f'Found {expired_count} expired temp bookings with status="reserved"')
        
        if expired_count > 0:
            if dry_run:
                self.stdout.write(self.style.WARNING(f'[DRY RUN] Would mark {expired_count} temp bookings as expired'))
                # Show some examples
                examples = expired_query[:5]
                for temp_booking in examples:
                    self.stdout.write(
                        f'  - {temp_booking.temp_id}: {temp_booking.buyer_phone}, '
                        f'expired at {temp_booking.expires_at}'
                    )
            else:
                updated = expired_query.update(status='expired')
                self.stdout.write(self.style.SUCCESS(f'✓ Marked {updated} temp bookings as expired'))
                logger.info(f'Marked {updated} temp bookings as expired')
        
        # Step 2: Delete old completed/expired TempBookings
        if delete_old:
            self.stdout.write(self.style.WARNING(f'\n=== Deleting Old TempBookings (older than {days_to_keep} days) ==='))
            
            cutoff_date = now - timedelta(days=days_to_keep)
            
            old_query = TempBooking.objects.filter(
                status__in=['completed', 'expired', 'cancelled'],
                created_at__lt=cutoff_date
            )
            
            old_count = old_query.count()
            self.stdout.write(f'Found {old_count} old temp bookings to delete')
            
            if old_count > 0:
                if dry_run:
                    self.stdout.write(self.style.WARNING(f'[DRY RUN] Would delete {old_count} old temp bookings'))
                    # Show breakdown by status
                    for status_val in ['completed', 'expired', 'cancelled']:
                        count = old_query.filter(status=status_val).count()
                        if count > 0:
                            self.stdout.write(f'  - {status_val}: {count}')
                else:
                    # Get counts before deletion for logging
                    completed_count = old_query.filter(status='completed').count()
                    expired_count = old_query.filter(status='expired').count()
                    cancelled_count = old_query.filter(status='cancelled').count()
                    
                    deleted_count, _ = old_query.delete()
                    self.stdout.write(self.style.SUCCESS(f'✓ Deleted {deleted_count} old temp bookings'))
                    self.stdout.write(f'  - completed: {completed_count}')
                    self.stdout.write(f'  - expired: {expired_count}')
                    self.stdout.write(f'  - cancelled: {cancelled_count}')
                    logger.info(
                        f'Deleted {deleted_count} old temp bookings '
                        f'(completed: {completed_count}, expired: {expired_count}, cancelled: {cancelled_count})'
                    )
        
        # Step 3: Show statistics
        self.stdout.write(self.style.WARNING('\n=== Current TempBooking Statistics ==='))
        
        total = TempBooking.objects.count()
        reserved = TempBooking.objects.filter(status='reserved').count()
        completed = TempBooking.objects.filter(status='completed').count()
        expired = TempBooking.objects.filter(status='expired').count()
        cancelled = TempBooking.objects.filter(status='cancelled').count()
        
        self.stdout.write(f'Total: {total}')
        self.stdout.write(f'  - reserved: {reserved}')
        self.stdout.write(f'  - completed: {completed}')
        self.stdout.write(f'  - expired: {expired}')
        self.stdout.write(f'  - cancelled: {cancelled}')
        
        # Check for reserved bookings that should be expired
        should_be_expired = TempBooking.objects.filter(
            status='reserved',
            expires_at__lte=now
        ).count()
        
        if should_be_expired > 0:
            self.stdout.write(self.style.ERROR(f'\n⚠ WARNING: {should_be_expired} reserved bookings are past expiry!'))
        
        self.stdout.write(self.style.SUCCESS('\n✓ Cleanup complete'))
