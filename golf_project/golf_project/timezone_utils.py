"""
Timezone utilities for Golf Project.

ARCHITECTURE:
- All datetimes in the DB are stored in UTC.
- Each GHLLocation has an IANA timezone string (e.g. 'America/Halifax').
- Use these helpers to convert between UTC and a center's local time.
- Wall-clock times (SimulatorAvailability, StaffAvailability, ClosedDay times)
  are stored as local time — always interpret them relative to the center's timezone.

WHY IANA NAMES (not fixed offsets):
- 'America/Halifax' automatically handles DST (UTC-4 in winter, UTC-3 in summer).
- Hardcoded offsets like `timedelta(hours=4)` break when clocks change.
"""

import pytz
from django.utils import timezone as django_tz
from datetime import datetime, date, time as dt_time

_FALLBACK_TZ = 'America/Halifax'


def get_center_timezone(location_id=None):
    """
    Get the pytz timezone object for a golf center by its GHL location_id.

    Falls back to America/Halifax if not found or location_id is None.
    This is DST-aware — DST transitions are handled automatically by pytz.

    Args:
        location_id: GHL location ID string, or None

    Returns:
        pytz.BaseTzInfo: Timezone object for the center
    """
    if not location_id:
        return pytz.timezone(_FALLBACK_TZ)

    try:
        from ghl.models import GHLLocation
        loc = GHLLocation.objects.get(location_id=location_id)
        tz_name = loc.timezone or _FALLBACK_TZ
        return pytz.timezone(tz_name)
    except Exception:
        return pytz.timezone(_FALLBACK_TZ)


def get_center_timezone_name(location_id=None):
    """
    Get the IANA timezone name string for a golf center.

    Args:
        location_id: GHL location ID string, or None

    Returns:
        str: IANA timezone name (e.g. 'America/Halifax')
    """
    if not location_id:
        return _FALLBACK_TZ

    try:
        from ghl.models import GHLLocation
        loc = GHLLocation.objects.get(location_id=location_id)
        return loc.timezone or _FALLBACK_TZ
    except Exception:
        return _FALLBACK_TZ


def utc_to_local(utc_dt, location_id=None):
    """
    Convert a UTC-aware datetime to the center's local datetime.

    Args:
        utc_dt: datetime object (aware or naive-UTC)
        location_id: GHL location ID string, or None for fallback

    Returns:
        datetime: Timezone-aware datetime in the center's local timezone
    """
    center_tz = get_center_timezone(location_id)
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        # Treat as UTC
        utc_dt = pytz.utc.localize(utc_dt)
    return utc_dt.astimezone(center_tz)


def local_to_utc(naive_local_dt, location_id=None):
    """
    Convert a naive local datetime (in the center's timezone) to UTC.

    This correctly handles DST — e.g. "March 8, 2:30 AM Halifax" maps to
    the right UTC regardless of whether DST has started.

    Args:
        naive_local_dt: datetime without tzinfo, interpreted as center's local time
        location_id: GHL location ID string, or None for fallback

    Returns:
        datetime: UTC-aware datetime
    """
    center_tz = get_center_timezone(location_id)
    if naive_local_dt is None:
        return None
    if naive_local_dt.tzinfo is not None:
        # Already aware — just convert to UTC
        return naive_local_dt.astimezone(pytz.utc)
    # Localize (DST-aware) then convert to UTC
    local_aware = center_tz.localize(naive_local_dt)
    return local_aware.astimezone(pytz.utc)


def get_today_local(location_id=None):
    """
    Get today's date in the center's local timezone.

    Args:
        location_id: GHL location ID string, or None

    Returns:
        datetime.date: Today's date in center's local timezone
    """
    now_utc = django_tz.now()
    return utc_to_local(now_utc, location_id).date()


def get_now_local(location_id=None):
    """
    Get current datetime in the center's local timezone.

    Args:
        location_id: GHL location ID string, or None

    Returns:
        datetime: Current moment in center's local timezone (timezone-aware)
    """
    now_utc = django_tz.now()
    return utc_to_local(now_utc, location_id)


def make_local_datetime(local_date, local_time, location_id=None):
    """
    Combine a local date and local wall-clock time into a UTC-aware datetime.

    Use this when you have stored wall-clock times (e.g. SimulatorAvailability,
    ClosedDay) and need to produce a proper UTC datetime for comparisons.

    Args:
        local_date: datetime.date object
        local_time: datetime.time object (wall-clock in center's timezone)
        location_id: GHL location ID string, or None

    Returns:
        datetime: UTC-aware datetime
    """
    naive = datetime.combine(local_date, local_time)
    return local_to_utc(naive, location_id)


def wall_clock_time_to_utc_for_date(wall_time, ref_date, location_id=None):
    """
    Convert a wall-clock time (stored local) to UTC for a specific date.

    This is critical for availability checks: "opens at 9 AM" stored as TimeField
    means 9 AM local time on ref_date, which maps to different UTC depending on DST.

    Args:
        wall_time: datetime.time — the stored local wall-clock time
        ref_date: datetime.date — the date this time refers to
        location_id: GHL location ID string, or None

    Returns:
        datetime: UTC-aware datetime
    """
    return make_local_datetime(ref_date, wall_time, location_id)


def validate_iana_timezone(tz_string):
    """
    Validate that a string is a valid IANA timezone name.

    Args:
        tz_string: String to validate

    Returns:
        bool: True if valid IANA timezone, False otherwise
    """
    try:
        pytz.timezone(tz_string)
        return True
    except pytz.UnknownTimeZoneError:
        return False


# Common IANA timezone choices for UI display
COMMON_TIMEZONES = [
    ('America/St_Johns', "St. John's (Newfoundland Time, UTC-3:30/−2:30)"),
    ('America/Halifax', "Halifax/Moncton (Atlantic Time, UTC-4/−3)"),
    ('America/Toronto', "Toronto/Montreal/Ottawa (Eastern Time, UTC-5/−4)"),
    ('America/Winnipeg', "Winnipeg/Regina (Central Time, UTC-6/−5)"),
    ('America/Edmonton', "Calgary/Edmonton (Mountain Time, UTC-7/−6)"),
    ('America/Vancouver', "Vancouver/Victoria (Pacific Time, UTC-8/−7)"),
    ('America/New_York', "New York (Eastern Time, UTC-5/−4)"),
    ('America/Chicago', "Chicago (Central Time, UTC-6/−5)"),
    ('America/Denver', "Denver (Mountain Time, UTC-7/−6)"),
    ('America/Los_Angeles', "Los Angeles (Pacific Time, UTC-8/−7)"),
    ('America/Phoenix', "Phoenix (Mountain Standard, UTC-7, no DST)"),
    ('Europe/London', "London (GMT/BST, UTC+0/+1)"),
    ('Europe/Paris', "Paris/Berlin (CET/CEST, UTC+1/+2)"),
    ('Europe/Dublin', "Dublin (IST/GMT, UTC+0/+1)"),
    ('Asia/Dubai', "Dubai (GST, UTC+4, no DST)"),
    ('Asia/Kolkata', "India (IST, UTC+5:30, no DST)"),
    ('Asia/Singapore', "Singapore (SGT, UTC+8, no DST)"),
    ('Asia/Tokyo', "Tokyo (JST, UTC+9, no DST)"),
    ('Australia/Sydney', "Sydney (AEST/AEDT, UTC+10/+11)"),
    ('Pacific/Auckland', "Auckland (NZST/NZDT, UTC+12/+13)"),
    ('UTC', "UTC (Universal Coordinated Time)"),
]
