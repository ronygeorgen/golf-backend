"""
Shared logic for closed days: finding bookings that overlap a closure period.
Uses center-local dates/times, converted to UTC for overlap checks.
Both conflict detection and preview/cancellation must use this to stay consistent.

STORAGE RULES (single source of truth):
- ClosedDay.start_date / end_date  -> DateField, stored as LOCAL calendar date
- ClosedDay.start_time / end_time  -> TimeField, stored as LOCAL wall-clock time
                                      NULL = full-day closure
- Booking.start_time / end_time    -> DateTimeField (UTC-aware via USE_TZ=True)

HOW WE COMPARE:
  1. Convert local closure date+time  →  UTC-aware datetime  (via make_local_datetime)
  2. Compare directly with booking UTC datetimes
  3. Overlap condition: booking_start < closure_end_utc  AND  booking_end > closure_start_utc
"""
import logging
from datetime import time as dt_time, timedelta
from django.db.models import Q

logger = logging.getLogger(__name__)


def get_bookings_for_closed_day(start_date, end_date, start_time=None, end_time=None, location_id=None):
    """
    Return list of Booking objects that overlap the given closed day range.
    Uses center-local wall-clock; converts to UTC for comparison with stored bookings.

    Args:
        start_date, end_date: date objects (center's LOCAL calendar date)
        start_time, end_time: time or "HH:MM" string, or None for full-day
        location_id: GHL location_id for timezone resolution and booking filtering

    Returns:
        list of Booking instances (confirmed/completed, overlapping the closure)
    """
    from golf_project.timezone_utils import make_local_datetime
    from bookings.models import Booking

    def to_time(val):
        """Normalise input to a naive datetime.time object, or None."""
        if val is None:
            return None
        if isinstance(val, dt_time):
            # Strip tzinfo if present (we always treat closure times as naive local)
            return val.replace(tzinfo=None) if getattr(val, 'tzinfo', None) else val
        if isinstance(val, str) and val.strip():
            parts = val.strip().split(':')
            try:
                return dt_time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
            except (ValueError, IndexError):
                return None
        return None

    start_time_t = to_time(start_time)
    end_time_t = to_time(end_time)

    # Partial closure ─ both start_time and end_time must be present
    is_partial = bool(start_time_t and end_time_t)

    logger.debug(
        "get_bookings_for_closed_day: start=%s end=%s start_time=%s end_time=%s partial=%s loc=%s",
        start_date, end_date, start_time_t, end_time_t, is_partial, location_id,
    )

    if is_partial:
        # ── Partial closure ──────────────────────────────────────────────
        # For each date in the range, build the UTC closure window and
        # collect all bookings that overlap that window.
        conflicting_bookings = []
        current_date = start_date
        all_closure_ranges_utc = []

        while current_date <= end_date:
            closure_start_utc = make_local_datetime(current_date, start_time_t, location_id)
            closure_end_utc = make_local_datetime(current_date, end_time_t, location_id)

            # Guard against end_time < start_time (overnight closure, rare but possible)
            if end_time_t <= start_time_t:
                closure_end_utc += timedelta(days=1)

            all_closure_ranges_utc.append((closure_start_utc, closure_end_utc))
            logger.debug(
                "  partial range for %s: %s  →  %s (UTC)",
                current_date, closure_start_utc.isoformat(), closure_end_utc.isoformat(),
            )
            current_date += timedelta(days=1)

        if not all_closure_ranges_utc:
            return []

        # Broad pre-filter across all windows so we hit the DB once
        first_start = min(r[0] for r in all_closure_ranges_utc)
        last_end = max(r[1] for r in all_closure_ranges_utc)

        bookings_qs = Booking.objects.filter(
            start_time__lt=last_end,
            end_time__gt=first_start,
            status__in=['confirmed', 'completed'],
        )
        if location_id:
            bookings_qs = bookings_qs.filter(
                Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
            )

        logger.debug(
            "  pre-filter UTC %s – %s → %d candidates",
            first_start.isoformat(), last_end.isoformat(), bookings_qs.count(),
        )

        # Per-booking: check exact overlap against EACH per-date window
        seen = set()
        for b in bookings_qs:
            if b.id in seen:
                continue
            for closure_start_utc, closure_end_utc in all_closure_ranges_utc:
                # Standard overlap: b.start < window_end  AND  b.end > window_start
                if b.start_time < closure_end_utc and b.end_time > closure_start_utc:
                    conflicting_bookings.append(b)
                    seen.add(b.id)
                    logger.debug(
                        "  MATCH booking %d: start=%s end=%s  window=%s–%s",
                        b.id, b.start_time.isoformat(), b.end_time.isoformat(),
                        closure_start_utc.isoformat(), closure_end_utc.isoformat(),
                    )
                    break
                else:
                    logger.debug(
                        "  skip booking %d: start=%s end=%s  window=%s–%s",
                        b.id, b.start_time.isoformat(), b.end_time.isoformat(),
                        closure_start_utc.isoformat(), closure_end_utc.isoformat(),
                    )

        logger.debug("  partial result: %d conflicting booking(s)", len(conflicting_bookings))
        return conflicting_bookings

    else:
        # ── Full-day closure ─────────────────────────────────────────────
        # Cover the entire local calendar day: midnight (00:00:00) → 23:59:59.999999
        # on each day in [start_date, end_date], converted to UTC.
        #
        # For Halifax (UTC-4):
        #   start_of_period_utc = start_date 00:00:00 local = start_date 04:00:00 UTC
        #   end_of_period_utc   = end_date   23:59:59 local = (end_date+1) 03:59:59 UTC
        #
        # Overlap condition: booking_start < end_utc  AND  booking_end > start_utc

        start_of_period_utc = make_local_datetime(start_date, dt_time.min, location_id)
        # Use dt_time.max (23:59:59.999999) so we cover the very last moment of the local day
        end_of_period_utc = make_local_datetime(end_date, dt_time.max, location_id)

        logger.debug(
            "  full-day UTC range: %s → %s",
            start_of_period_utc.isoformat(), end_of_period_utc.isoformat(),
        )

        bookings_qs = Booking.objects.filter(
            start_time__lt=end_of_period_utc,
            end_time__gt=start_of_period_utc,
            status__in=['confirmed', 'completed'],
        )
        if location_id:
            bookings_qs = bookings_qs.filter(
                Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id='')
            )

        result = list(bookings_qs)
        logger.debug("  full-day result: %d conflicting booking(s)", len(result))
        return result
