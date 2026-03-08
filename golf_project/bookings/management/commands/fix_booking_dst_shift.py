"""
Fix 1-hour DST shift for bookings stored with wrong offset (AST vs ADT).

SCENARIO: Old code used fixed UTC-4 (AST) year-round. Summer bookings (ADT period)
were stored 1hr too late. This subtracts 1hr from those bookings.

Run:
  python manage.py fix_booking_dst_shift --dry-run     # Preview (no changes)
  python manage.py fix_booking_dst_shift --apply       # Apply changes

AFFECTS: OLD/EXISTING bookings only. New bookings created after your code fix
are stored correctly and are NOT modified.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from bookings.models import Booking
import pytz


# America/Halifax DST: Mar 8 - Nov 1 (approx). Use pytz to detect ADT.
HALIFAX = pytz.timezone('America/Halifax')


def is_in_adt_period(dt):
    """True if datetime (UTC) falls in Halifax ADT (summer) period."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    local = dt.astimezone(HALIFAX)
    return local.dst() is not None and local.dst().total_seconds() != 0


def is_in_ast_period(dt):
    """True if datetime (UTC) falls in Halifax AST (winter) period."""
    return not is_in_adt_period(dt)


class Command(BaseCommand):
    help = "Fix 1-hour DST shift for bookings stored with wrong offset (AST vs ADT)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=True,
            help='Preview changes without applying (default)',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Apply the fix to the database',
        )
        parser.add_argument(
            '--mode',
            choices=['fix-summer', 'fix-winter'],
            default='fix-summer',
            help='fix-summer: subtract 1hr for ADT-period bookings (were stored with AST). fix-winter: add 1hr for AST-period.',
        )
        parser.add_argument(
            '--location-id',
            type=str,
            default=None,
            help='Only fix bookings for this location',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Max number of bookings to fix (for testing)',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        dry_run = not apply
        mode = options['mode']
        location_id = options.get('location_id')
        limit = options.get('limit')

        if not apply:
            self.stdout.write(self.style.WARNING('DRY RUN - No changes will be made. Use --apply to fix.\n'))

        delta = timedelta(hours=-1) if mode == 'fix-summer' else timedelta(hours=1)
        period_name = 'ADT (summer)' if mode == 'fix-summer' else 'AST (winter)'

        self.stdout.write(f'Mode: {mode} (adjust {period_name} bookings by {"-1hr" if mode == "fix-summer" else "+1hr"})')
        self.stdout.write('')

        qs = Booking.objects.filter(status__in=['confirmed', 'completed'])
        if location_id:
            qs = qs.filter(location_id=location_id)

        to_fix = []
        for b in qs.iterator():
            if mode == 'fix-summer' and is_in_adt_period(b.start_time):
                to_fix.append(b)
            elif mode == 'fix-winter' and is_in_ast_period(b.start_time):
                to_fix.append(b)
            if limit and len(to_fix) >= limit:
                break

        if not to_fix:
            self.stdout.write(self.style.SUCCESS('No bookings need fixing.'))
            return

        self.stdout.write(f'Found {len(to_fix)} booking(s) to fix:\n')
        for b in to_fix[:20]:
            client = f"{b.client.first_name or ''} {b.client.last_name or ''}".strip() or b.client.username
            new_start = b.start_time + delta
            new_end = b.end_time + delta
            self.stdout.write(
                f'  ID {b.id} | {b.booking_type} | {client} | '
                f'{b.start_time.strftime("%Y-%m-%d %H:%M")} -> {new_start.strftime("%Y-%m-%d %H:%M")}'
            )
        if len(to_fix) > 20:
            self.stdout.write(f'  ... and {len(to_fix) - 20} more')

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDRY RUN: {len(to_fix)} booking(s) would be updated. Run with --apply to fix.'))
            return

        updated = 0
        with transaction.atomic():
            for b in to_fix:
                b.start_time = b.start_time + delta
                b.end_time = b.end_time + delta
                b.save(update_fields=['start_time', 'end_time', 'updated_at'])
                updated += 1

        self.stdout.write(self.style.SUCCESS(f'\nUpdated {updated} booking(s).'))
        self.stdout.write('\nRun: python manage.py check_booking_timezone --limit 10 to verify.')
