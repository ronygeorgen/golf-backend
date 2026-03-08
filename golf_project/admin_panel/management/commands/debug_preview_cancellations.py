"""
Debug script to verify that closed-day cancellation logic correctly identifies
bookings to cancel for a given closure date/time.

This command mirrors EXACTLY what the frontend sends and the backend uses:
  - Frontend sends: start_date, end_date, start_time, end_time, location_id (as local wall-clock)
  - Backend converts those local times to UTC and queries bookings by overlap

USAGE EXAMPLES:
  # Full-day closure on March 18 2026
  python manage.py debug_preview_cancellations --date 2026-03-18

  # Full-day closure with explicit location
  python manage.py debug_preview_cancellations --date 2026-03-18 --location-id IN0bpFDCWfBrDlUIvQB6

  # Partial closure 4PM–7PM on March 18 2026
  python manage.py debug_preview_cancellations --date 2026-03-18 --start-time 16:00 --end-time 19:00

  # Multi-day range
  python manage.py debug_preview_cancellations --start-date 2026-03-18 --end-date 2026-03-20
"""

from django.core.management.base import BaseCommand
from django.db.models import Q
from datetime import date, time as dt_time
from bookings.models import Booking
from admin_panel.models import ClosedDay
from admin_panel.closed_days_utils import get_bookings_for_closed_day
from golf_project.timezone_utils import (
    get_center_timezone,
    get_center_timezone_name,
    make_local_datetime,
    utc_to_local,
    local_to_utc,
)


class Command(BaseCommand):
    help = 'Debug closed-day cancellation logic — mirrors actual frontend→backend flow'

    def add_arguments(self, parser):
        parser.add_argument('--date', default=None,
                            help='Single date YYYY-MM-DD (sets both start-date and end-date)')
        parser.add_argument('--start-date', default=None, help='Start date YYYY-MM-DD')
        parser.add_argument('--end-date', default=None, help='End date YYYY-MM-DD')
        parser.add_argument('--start-time', default=None,
                            help='Partial closure start time HH:MM (local). Omit for full-day.')
        parser.add_argument('--end-time', default=None,
                            help='Partial closure end time HH:MM (local). Omit for full-day.')
        parser.add_argument('--location-id', default=None,
                            help='GHL location_id (uses first GHLLocation if omitted)')

    def handle(self, *args, **options):
        location_id = options.get('location_id')

        # Resolve location_id if not provided
        if not location_id:
            try:
                from ghl.models import GHLLocation
                first_loc = GHLLocation.objects.first()
                if first_loc:
                    location_id = first_loc.location_id
                    self.stdout.write(self.style.WARNING(
                        f'No --location-id provided. Using first GHLLocation: {location_id}'
                    ))
                else:
                    self.stdout.write(self.style.ERROR('No GHLLocation found. Provide --location-id'))
                    return
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Could not get location: {e}'))
                return

        # Parse dates
        date_str = options.get('date')
        start_date_str = options.get('start_date') or date_str
        end_date_str = options.get('end_date') or date_str

        if not start_date_str:
            self.stdout.write(self.style.ERROR('Provide --date or --start-date'))
            return

        try:
            start_date = date.fromisoformat(start_date_str)
            end_date = date.fromisoformat(end_date_str) if end_date_str else start_date
        except ValueError as e:
            self.stdout.write(self.style.ERROR(f'Invalid date: {e}'))
            return

        # Parse times
        start_time_str = options.get('start_time')
        end_time_str = options.get('end_time')

        def parse_time(s):
            if not s:
                return None
            parts = s.split(':')
            return dt_time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

        start_time = parse_time(start_time_str)
        end_time = parse_time(end_time_str)
        is_partial = bool(start_time and end_time)

        tz_name = get_center_timezone_name(location_id)
        center_tz = get_center_timezone(location_id)

        # ── Header ────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS('\n=== CLOSED DAY DEBUG ==='))
        self.stdout.write(f'  location_id  : {location_id!r}')
        self.stdout.write(f'  timezone     : {tz_name}')
        self.stdout.write(f'  start_date   : {start_date} (LOCAL)')
        self.stdout.write(f'  end_date     : {end_date} (LOCAL)')
        self.stdout.write(f'  closure_type : {"PARTIAL " + str(start_time) + "–" + str(end_time) if is_partial else "FULL-DAY"}')

        # ── UTC ranges ────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS('\n=== UTC RANGES USED IN QUERY ==='))
        if is_partial:
            cur = start_date
            from datetime import timedelta
            while cur <= end_date:
                cs = make_local_datetime(cur, start_time, location_id)
                ce = make_local_datetime(cur, end_time, location_id)
                self.stdout.write(
                    f'  {cur} LOCAL {start_time}–{end_time}  →  UTC {cs.isoformat()} – {ce.isoformat()}'
                )
                cur += timedelta(days=1)
        else:
            start_utc = make_local_datetime(start_date, dt_time.min, location_id)
            end_utc = make_local_datetime(end_date, dt_time.max, location_id)
            self.stdout.write(f'  start_utc = {start_utc.isoformat()}')
            self.stdout.write(f'  end_utc   = {end_utc.isoformat()}')

        # ── All bookings in the raw UTC window ────────────────────────────
        self.stdout.write(self.style.SUCCESS('\n=== RAW BOOKINGS IN UTC WINDOW (before location filter) ==='))
        if is_partial:
            from datetime import timedelta
            first_start_utc = make_local_datetime(start_date, start_time, location_id)
            last_end_utc = make_local_datetime(end_date, end_time, location_id)
        else:
            first_start_utc = make_local_datetime(start_date, dt_time.min, location_id)
            last_end_utc = make_local_datetime(end_date, dt_time.max, location_id)

        raw_qs = Booking.objects.filter(
            start_time__lt=last_end_utc,
            end_time__gt=first_start_utc,
            status__in=['confirmed', 'completed'],
        )
        self.stdout.write(f'  Total (all locations): {raw_qs.count()}')

        loc_qs = raw_qs.filter(
            Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
        )
        self.stdout.write(f'  After location filter: {loc_qs.count()}')

        for b in loc_qs[:10]:
            local_s = utc_to_local(b.start_time, location_id)
            local_e = utc_to_local(b.end_time, location_id)
            self.stdout.write(
                f'    id={b.id} type={b.booking_type} loc={b.location_id!r} '
                f'UTC {b.start_time.isoformat()} – {b.end_time.isoformat()} | '
                f'LOCAL {local_s.strftime("%Y-%m-%d %H:%M")} – {local_e.strftime("%H:%M")}'
            )
        if loc_qs.count() > 10:
            self.stdout.write(f'    … and {loc_qs.count() - 10} more')

        # ── get_bookings_for_closed_day result ────────────────────────────
        self.stdout.write(self.style.SUCCESS('\n=== get_bookings_for_closed_day RESULT ==='))
        result = get_bookings_for_closed_day(start_date, end_date, start_time, end_time, location_id)
        self.stdout.write(f'  Bookings to cancel: {len(result)}')
        for b in result:
            local_s = utc_to_local(b.start_time, location_id)
            local_e = utc_to_local(b.end_time, location_id)
            self.stdout.write(
                f'    id={b.id} type={b.booking_type} status={b.status} '
                f'LOCAL {local_s.strftime("%Y-%m-%d %H:%M")} – {local_e.strftime("%H:%M")}'
            )

        # ── Active ClosedDay records that cover this date ─────────────────
        self.stdout.write(self.style.SUCCESS('\n=== ACTIVE CLOSED DAYS COVERING THIS DATE RANGE ==='))
        closed_qs = ClosedDay.objects.filter(
            start_date__lte=end_date,
            end_date__gte=start_date,
            is_active=True,
        ).filter(
            Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
        )
        self.stdout.write(f'  Count: {closed_qs.count()}')
        for c in closed_qs[:5]:
            self.stdout.write(
                f'    id={c.id} "{c.title}" {c.start_date}→{c.end_date} '
                f'times={c.start_time}–{c.end_time} loc={c.location_id!r}'
            )

        self.stdout.write('')
        if len(result) == 0:
            self.stdout.write(self.style.WARNING(
                'No bookings would be cancelled. If you expected some:\n'
                '  1. Check tz offset: are UTC ranges correct for your timezone?\n'
                '  2. Check location_id: does it match bookings.location_id?\n'
                '  3. Check booking status: must be confirmed or completed.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f'✓ {len(result)} booking(s) would be cancelled.'))
