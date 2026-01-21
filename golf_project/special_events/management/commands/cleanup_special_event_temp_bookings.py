from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from special_events.models import TempSpecialEventBooking
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up expired temporary special event bookings'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete-old',
            action='store_true',
            help='Delete old completed/expired temp event bookings',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days to keep completed/expired mappings (default: 7)',
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
        
        # Step 1: Mark expired TempSpecialEventBookings
        self.stdout.write(self.style.WARNING('\n=== Marking Expired Temp Event Bookings ==='))
        
        expired_query = TempSpecialEventBooking.objects.filter(
            status='reserved',
            expires_at__lte=now
        )
        
        expired_count = expired_query.count()
        self.stdout.write(f'Found {expired_count} expired temp event bookings with status="reserved"')
        
        if expired_count > 0:
            if dry_run:
                self.stdout.write(self.style.WARNING(f'[DRY RUN] Would mark {expired_count} temp event bookings as expired'))
            else:
                updated = expired_query.update(status='expired')
                self.stdout.write(self.style.SUCCESS(f'✓ Marked {updated} temp event bookings as expired'))
                logger.info(f'Marked {updated} temp event bookings as expired')
        
        # Step 2: Delete old completed/expired TempSpecialEventBookings
        if delete_old:
            self.stdout.write(self.style.WARNING(f'\n=== Deleting Old Temp Event Bookings (older than {days_to_keep} days) ==='))
            
            cutoff_date = now - timedelta(days=days_to_keep)
            
            old_query = TempSpecialEventBooking.objects.filter(
                status__in=['completed', 'expired', 'cancelled'],
                created_at__lt=cutoff_date
            )
            
            old_count = old_query.count()
            self.stdout.write(f'Found {old_count} old temp event bookings to delete')
            
            if old_count > 0:
                if dry_run:
                    self.stdout.write(self.style.WARNING(f'[DRY RUN] Would delete {old_count} old temp event bookings'))
                else:
                    deleted_count, _ = old_query.delete()
                    self.stdout.write(self.style.SUCCESS(f'✓ Deleted {deleted_count} old temp event bookings'))
                    logger.info(f'Deleted {deleted_count} old temp event bookings')
        
        # Step 3: Show statistics
        self.stdout.write(self.style.WARNING('\n=== Current TempSpecialEventBooking Statistics ==='))
        
        total = TempSpecialEventBooking.objects.count()
        reserved = TempSpecialEventBooking.objects.filter(status='reserved').count()
        completed = TempSpecialEventBooking.objects.filter(status='completed').count()
        expired = TempSpecialEventBooking.objects.filter(status='expired').count()
        
        self.stdout.write(f'Total: {total}')
        self.stdout.write(f'  - reserved: {reserved}')
        self.stdout.write(f'  - completed: {completed}')
        self.stdout.write(f'  - expired: {expired}')
        
        self.stdout.write(self.style.SUCCESS('\n✓ Cleanup complete'))
