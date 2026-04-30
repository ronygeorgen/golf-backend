"""
Phase E — Category slot availability engine.

For new service categories (legacy_booking_type=None), staff members are linked
via StaffCategory rather than CoachingPackage.staff_members.  No simulator/bay
is required; availability is purely coach-schedule based.

The returned slot structure mirrors check_coaching_availability so the frontend
can reuse the same rendering logic:

    {
        "start_time": "<ISO UTC>",
        "end_time": "<ISO UTC>",
        "duration_minutes": 60,
        "availability_end_time": "<ISO UTC>",
        "fits_duration": true,
        "available_coaches": [
            {"id": 1, "name": "Alice Smith", "email": "alice@example.com"}
        ]
    }
"""

from datetime import datetime, timedelta

import pytz
from django.db.models import Q
from django.utils import timezone


def compute_category_slots(
    category_id,
    booking_date,
    location_id,
    package=None,
    coach_id=None,
    asset_id=None,
):
    """
    Return a list of available time slots for a service category on a given date.

    Args:
        category_id  : int  – ServiceCategory PK
        booking_date : date – the local calendar date to query
        location_id  : str  – GHL location ID (may be empty/None)
        package      : CoachingPackage instance or None – restricts staff to
                       intersection of StaffCategory and package.staff_members.
        coach_id     : int or None – restrict to one coach (staff-based assets)
        asset_id     : int or None – specific CategoryAsset to check.
                       If the asset has needs_staff=False, asset-schedule logic is used.
                       If needs_staff=True (or no asset), staff-schedule logic is used.
                       If None and the category has assets, staff-based logic applies
                       (caller should always pass asset_id when assets are defined).

    Returns:
        list[dict] sorted by start_time ISO string.
        Asset-based slots include an 'asset' key with {id, name, price_per_hour}.
        Staff-based slots include 'available_coaches' as before.
    """
    from users.models import (
        StaffAvailability,
        StaffBlockedDate,
        StaffCategory,
        StaffDayAvailability,
        User,
    )
    from bookings.models import Booking
    from golf_project.timezone_utils import get_center_timezone
    from .models import CategoryAsset, CategoryAssetAvailability

    center_tz = get_center_timezone(location_id)
    day_of_week = booking_date.weekday()

    # ------------------------------------------------------------------ #
    # 0.  Asset-based slot logic (needs_staff=False)                      #
    # ------------------------------------------------------------------ #
    if asset_id:
        try:
            asset = CategoryAsset.objects.get(pk=asset_id, category_id=category_id, is_active=True)
        except CategoryAsset.DoesNotExist:
            return []

        # If this asset requires staff, fall through to the normal staff logic below.
        if not asset.needs_staff:
            return _compute_asset_slots(
                asset=asset,
                booking_date=booking_date,
                day_of_week=day_of_week,
                center_tz=center_tz,
                location_id=location_id,
                package=package,
            )
        # needs_staff=True: continue into staff logic with this asset noted

    # ------------------------------------------------------------------ #
    # 1.  Resolve candidate coaches                                        #
    # ------------------------------------------------------------------ #
    # Start from staff assigned to this category
    cat_staff_ids = list(
        StaffCategory.objects.filter(category_id=category_id)
        .values_list('staff_id', flat=True)
    )
    coaches_qs = User.objects.filter(
        id__in=cat_staff_ids,
        role__in=['staff', 'admin'],
        is_active=True,
    )
    if location_id:
        coaches_qs = coaches_qs.filter(ghl_location_id=location_id)

    # If the package explicitly declares staff, intersect
    if package is not None:
        pkg_staff_ids = list(
            package.staff_members.values_list('id', flat=True)
        )
        if pkg_staff_ids:
            coaches_qs = coaches_qs.filter(id__in=pkg_staff_ids)

    if coach_id:
        coaches_qs = coaches_qs.filter(id=coach_id)

    coaches = list(coaches_qs.distinct())
    if not coaches:
        return []

    # ------------------------------------------------------------------ #
    # 2.  Determine session duration                                       #
    # ------------------------------------------------------------------ #
    duration_minutes = 60  # default
    if package is not None and hasattr(package, 'session_duration_minutes'):
        duration_minutes = package.session_duration_minutes or 60

    # ------------------------------------------------------------------ #
    # 3.  Build UTC window for prefetch queries                           #
    # ------------------------------------------------------------------ #
    booking_day_utc_start = pytz.UTC.localize(
        datetime(booking_date.year, booking_date.month, booking_date.day, 3, 0, 0)
    )
    booking_day_utc_end = booking_day_utc_start + timedelta(days=1, hours=3)

    # Prefetch existing confirmed/completed bookings (coach-conflict check)
    relevant_bookings = list(
        Booking.objects.filter(
            start_time__lt=booking_day_utc_end,
            end_time__gt=booking_day_utc_start,
            status__in=['confirmed', 'completed'],
        ).filter(
            Q(location_id=location_id) if location_id else Q()
        ).select_related('coach')
    )

    # ------------------------------------------------------------------ #
    # 4.  Facility closures & special events                              #
    # ------------------------------------------------------------------ #
    from admin_panel.models import ClosedDay
    from special_events.models import SpecialEvent

    active_closures = list(
        ClosedDay.objects.filter(is_active=True).filter(
            Q(location_id=location_id) | Q(location_id__isnull=True)
            if location_id else Q()
        )
    )

    next_day = booking_date + timedelta(days=1)
    # For staff-based slots: only facility-wide events block (exclude asset group events)
    day_events_qs = SpecialEvent.objects.filter(is_active=True, category_asset__isnull=True)
    if location_id:
        day_events_qs = day_events_qs.filter(location_id=location_id)
    day_events = [
        e for e in day_events_qs
        if e.get_occurrences(start_date=booking_date, end_date=next_day)
    ]

    def is_facility_closed(check_time):
        is_closed, _ = ClosedDay.check_if_closed(check_time, location_id=location_id)
        return is_closed

    def has_special_event_conflict(slot_start, slot_end):
        for event in day_events:
            if event.conflicts_with_range(slot_start, slot_end):
                return True
        return False

    # ------------------------------------------------------------------ #
    # 5.  Build per-coach availability and blocked-time maps (UTC)        #
    # ------------------------------------------------------------------ #
    availability_by_staff = {}   # coach.id -> [(s_utc, e_utc), ...]
    blocked_by_staff = {}        # coach.id -> [(s_utc, e_utc), ...]

    for coach in coaches:
        # ── Full-day block check ─────────────────────────────────────────── #
        # A coach is fully blocked if there is a full-day block that applies
        # to this category (service_category=category_id) OR to all categories
        # (service_category=NULL).
        if StaffBlockedDate.objects.filter(
            Q(service_category_id=category_id) | Q(service_category__isnull=True),
            staff=coach,
            date=booking_date,
            start_time__isnull=True,
            end_time__isnull=True,
        ).exists():
            continue  # skip this coach entirely

        # ── Partial blocks ───────────────────────────────────────────────── #
        # Collect partial blocks that match this category OR are general (NULL).
        partial = list(
            StaffBlockedDate.objects.filter(
                Q(service_category_id=category_id) | Q(service_category__isnull=True),
                staff=coach,
                date=booking_date,
                start_time__isnull=False,
                end_time__isnull=False,
            ).values('start_time', 'end_time')
        )
        if partial:
            utc_blocks = []
            for b in partial:
                s_naive = datetime.combine(booking_date, b['start_time'])
                e_naive = datetime.combine(booking_date, b['end_time'])
                if b['end_time'] <= b['start_time']:
                    e_naive += timedelta(days=1)
                utc_blocks.append((
                    center_tz.localize(s_naive).astimezone(pytz.UTC),
                    center_tz.localize(e_naive).astimezone(pytz.UTC),
                ))
            blocked_by_staff[coach.id] = utc_blocks

        # ── Availability windows ─────────────────────────────────────────── #
        # Resolution order (most-specific first):
        #   1. Day-specific availability for this category
        #   2. Day-specific general availability (NULL category)
        #   3. Recurring availability for this category
        #   4. Recurring general availability (NULL category)
        avails = list(
            StaffDayAvailability.objects.filter(
                staff=coach,
                date=booking_date,
                service_category_id=category_id,
            ).values('start_time', 'end_time')
        )
        if not avails:
            avails = list(
                StaffDayAvailability.objects.filter(
                    staff=coach,
                    date=booking_date,
                    service_category__isnull=True,
                ).values('start_time', 'end_time')
            )
        if not avails:
            avails = list(
                StaffAvailability.objects.filter(
                    staff=coach,
                    day_of_week=day_of_week,
                    service_category_id=category_id,
                ).values('start_time', 'end_time')
            )
        if not avails:
            avails = list(
                StaffAvailability.objects.filter(
                    staff=coach,
                    day_of_week=day_of_week,
                    service_category__isnull=True,
                ).values('start_time', 'end_time')
            )
        if avails:
            utc_avail = []
            for a in avails:
                s_naive = datetime.combine(booking_date, a['start_time'])
                e_naive = datetime.combine(booking_date, a['end_time'])
                if a['end_time'] <= a['start_time']:
                    e_naive += timedelta(days=1)
                utc_avail.append((
                    center_tz.localize(s_naive).astimezone(pytz.UTC),
                    center_tz.localize(e_naive).astimezone(pytz.UTC),
                ))
            availability_by_staff[coach.id] = utc_avail

    if not availability_by_staff:
        return []

    # ------------------------------------------------------------------ #
    # 6.  Determine search window from union of coach shifts              #
    # ------------------------------------------------------------------ #
    min_start_utc = None
    max_end_utc = None
    for avail_list in availability_by_staff.values():
        for s_utc, e_utc in avail_list:
            if min_start_utc is None or s_utc < min_start_utc:
                min_start_utc = s_utc
            if max_end_utc is None or e_utc > max_end_utc:
                max_end_utc = e_utc

    if not min_start_utc:
        return []

    # ------------------------------------------------------------------ #
    # 7.  Generate slots                                                  #
    # ------------------------------------------------------------------ #
    slot_interval = 30
    now = timezone.now()
    available_slots_map = {}
    current_slot_start = min_start_utc

    while current_slot_start < max_end_utc:
        slot_start = current_slot_start
        slot_end = slot_start + timedelta(minutes=duration_minutes)

        # Skip past slots
        if slot_start <= now:
            current_slot_start += timedelta(minutes=slot_interval)
            continue

        # Facility checks
        if is_facility_closed(slot_start):
            current_slot_start += timedelta(minutes=slot_interval)
            continue

        if has_special_event_conflict(slot_start, slot_end):
            current_slot_start += timedelta(minutes=slot_interval)
            continue

        # Find coaches available for this slot
        slot_coaches = []
        for coach in coaches:
            coach_avail = availability_by_staff.get(coach.id, [])

            # Must be on shift for the full slot
            shift_end = None
            for s_utc, e_utc in coach_avail:
                if s_utc <= slot_start and e_utc >= slot_end:
                    shift_end = e_utc
                    break
            if shift_end is None:
                continue

            # Must not be in a blocked window
            is_blocked = False
            for b_start, b_end in blocked_by_staff.get(coach.id, []):
                if slot_start < b_end and slot_end > b_start:
                    is_blocked = True
                    break
            if is_blocked:
                continue

            # Must not have an existing booking overlap
            is_booked = False
            for b in relevant_bookings:
                if b.coach_id == coach.id and b.start_time < slot_end and b.end_time > slot_start:
                    is_booked = True
                    break
            if is_booked:
                continue

            slot_coaches.append((coach, shift_end))

        if slot_coaches:
            slot_key = slot_start.isoformat()
            entry = available_slots_map.setdefault(slot_key, {
                'start_time': slot_key,
                'end_time': slot_end.isoformat(),
                'duration_minutes': duration_minutes,
                'availability_end_time': slot_coaches[0][1].isoformat(),
                'fits_duration': True,
                'available_coaches': [],
            })
            for coach, shift_end in slot_coaches:
                coach_name = f"{coach.first_name} {coach.last_name}".strip() or coach.username
                entry['available_coaches'].append({
                    'id': coach.id,
                    'name': coach_name,
                    'email': coach.email,
                })
                if shift_end.isoformat() > entry['availability_end_time']:
                    entry['availability_end_time'] = shift_end.isoformat()

        current_slot_start += timedelta(minutes=slot_interval)

    return sorted(available_slots_map.values(), key=lambda x: x['start_time'])


# ---------------------------------------------------------------------------
# Asset-based slot engine (needs_staff=False)
# ---------------------------------------------------------------------------

def _compute_asset_slots(asset, booking_date, day_of_week, center_tz, location_id, package=None):
    """
    Generate available slots for a single needs_staff=False CategoryAsset.

    A slot is available when:
      1. The asset's weekly schedule covers the full slot window.
      2. No confirmed/completed Booking already occupies that asset on this slot.
      3. The facility is open (ClosedDay + SpecialEvent checks).

    Slot structure mirrors compute_category_slots so the frontend can share
    rendering code; 'available_coaches' is an empty list, and an 'asset' key
    is added with asset metadata.
    """
    import pytz
    from datetime import datetime, timedelta
    from django.db.models import Q
    from django.utils import timezone as tz

    from bookings.models import Booking
    from admin_panel.models import ClosedDay
    from special_events.models import SpecialEvent

    duration_minutes = 60
    if package is not None and hasattr(package, 'session_duration_minutes'):
        duration_minutes = package.session_duration_minutes or 60

    # Load the asset's weekly availability for this day
    avail_windows = list(
        asset.availabilities.filter(day_of_week=day_of_week).values('start_time', 'end_time')
    )
    if not avail_windows:
        return []

    # Convert availability windows to UTC
    utc_windows = []
    for w in avail_windows:
        s_naive = datetime.combine(booking_date, w['start_time'])
        e_naive = datetime.combine(booking_date, w['end_time'])
        if w['end_time'] <= w['start_time']:
            e_naive += timedelta(days=1)
        utc_windows.append((
            center_tz.localize(s_naive).astimezone(pytz.UTC),
            center_tz.localize(e_naive).astimezone(pytz.UTC),
        ))

    if not utc_windows:
        return []

    min_start = min(s for s, _ in utc_windows)
    max_end = max(e for _, e in utc_windows)

    # Prefetch existing bookings for this asset on this day
    existing_bookings = list(
        Booking.objects.filter(
            category_asset=asset,
            start_time__lt=max_end,
            end_time__gt=min_start,
            status__in=['confirmed', 'completed'],
        )
    )

    # Facility closure helpers
    active_closures = list(
        ClosedDay.objects.filter(is_active=True).filter(
            Q(location_id=location_id) | Q(location_id__isnull=True) if location_id else Q()
        )
    )
    next_day = booking_date + timedelta(days=1)
    # Block on: (a) facility-wide events (category_asset is null), OR
    #           (b) group events for THIS specific asset.
    # Do NOT block on group events linked to OTHER assets.
    from django.db.models import Q as DQ
    day_events_qs = SpecialEvent.objects.filter(
        is_active=True
    ).filter(
        DQ(category_asset__isnull=True) | DQ(category_asset=asset)
    )
    if location_id:
        day_events_qs = day_events_qs.filter(location_id=location_id)
    day_events = [e for e in day_events_qs if e.get_occurrences(start_date=booking_date, end_date=next_day)]

    def is_facility_closed(check_time):
        closed, _ = ClosedDay.check_if_closed(check_time, location_id=location_id)
        return closed

    def has_event_conflict(slot_start, slot_end):
        return any(e.conflicts_with_range(slot_start, slot_end) for e in day_events)

    def in_availability(slot_start, slot_end):
        return any(s <= slot_start and e >= slot_end for s, e in utc_windows)

    now = tz.now()
    slot_interval = 30
    current = min_start
    slots = {}

    while current < max_end:
        slot_end = current + timedelta(minutes=duration_minutes)

        if current <= now:
            current += timedelta(minutes=slot_interval)
            continue

        if not in_availability(current, slot_end):
            current += timedelta(minutes=slot_interval)
            continue

        if is_facility_closed(current):
            current += timedelta(minutes=slot_interval)
            continue

        if has_event_conflict(current, slot_end):
            current += timedelta(minutes=slot_interval)
            continue

        # Check no existing booking overlaps
        is_booked = any(
            b.start_time < slot_end and b.end_time > current
            for b in existing_bookings
        )
        if not is_booked:
            key = current.isoformat()
            slots[key] = {
                'start_time': key,
                'end_time': slot_end.isoformat(),
                'duration_minutes': duration_minutes,
                'availability_end_time': max(e for s, e in utc_windows if s <= current).isoformat(),
                'fits_duration': True,
                'available_coaches': [],
                'asset': {
                    'id': asset.id,
                    'name': asset.name,
                    'price_per_hour': str(asset.price_per_hour) if asset.price_per_hour else None,
                },
            }

        current += timedelta(minutes=slot_interval)

    return sorted(slots.values(), key=lambda x: x['start_time'])
