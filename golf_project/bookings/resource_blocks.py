"""
One-off resource blackouts (bay / category asset) — shared for slot listing and create.

Block date + start/end are center-local wall clock.
Booking start/end are UTC-aware DateTimeFields.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _local_window(start_time, end_time, location_id):
    from golf_project.timezone_utils import get_center_timezone

    center_tz = get_center_timezone(location_id)
    local_start = start_time.astimezone(center_tz)
    local_end = end_time.astimezone(center_tz)
    return local_start.date(), local_start.time(), local_end.time()


def is_simulator_blocked(simulator, start_time, end_time) -> bool:
    """True if a SimulatorBlockedDate overlaps this UTC window."""
    if not simulator or not start_time or not end_time:
        return False
    from simulators.models import SimulatorBlockedDate

    location_id = getattr(simulator, 'location_id', None)
    booking_date, local_start_t, local_end_t = _local_window(start_time, end_time, location_id)
    for blk in SimulatorBlockedDate.objects.filter(simulator=simulator, date=booking_date):
        if blk.conflicts_with_time(local_start_t, local_end_t):
            return True
    return False


def is_category_asset_blocked(asset, start_time, end_time) -> bool:
    """True if a CategoryAssetBlockedDate overlaps this UTC window."""
    if not asset or not start_time or not end_time:
        return False
    from categories.models import CategoryAssetBlockedDate

    location_id = getattr(asset, 'location_id', None)
    booking_date, local_start_t, local_end_t = _local_window(start_time, end_time, location_id)
    for blk in CategoryAssetBlockedDate.objects.filter(asset=asset, date=booking_date):
        if blk.conflicts_with_time(local_start_t, local_end_t):
            return True
    return False
