"""
Cancel overlapping bookings when a resource blackout is created.

Timezone rules (see golf_project.timezone_utils):
- Booking.start_time / end_time are stored in UTC (aware DateTimeField).
- Block date + start_time/end_time are center-local wall clock (naive TimeField / DateField).
- Convert block window to aware center-local datetimes, then compare to booking UTC times.
"""
from __future__ import annotations

import logging
from datetime import datetime as dt, time as dt_time
from decimal import Decimal

from django.db import transaction
from django.db.models import F

logger = logging.getLogger(__name__)


def _center_day_bounds(block_date, location_id):
    from golf_project.timezone_utils import get_center_timezone

    center_tz = get_center_timezone(location_id)
    start_of_day = center_tz.localize(dt.combine(block_date, dt_time.min))
    end_of_day = center_tz.localize(dt.combine(block_date, dt_time.max))
    return center_tz, start_of_day, end_of_day


def _block_window(block_date, start_time, end_time, location_id):
    """
    Return (center_tz, block_start_dt, block_end_dt) as aware datetimes in center TZ.
    Full-day => start/end of local calendar day.
    """
    center_tz, start_of_day, end_of_day = _center_day_bounds(block_date, location_id)
    if start_time and end_time:
        return (
            center_tz,
            center_tz.localize(dt.combine(block_date, start_time)),
            center_tz.localize(dt.combine(block_date, end_time)),
        )
    return center_tz, start_of_day, end_of_day


def find_overlapping_bookings(qs, block_date, start_time, end_time, location_id):
    """
    Filter a Booking queryset to confirmed bookings on the local day that overlap
    the block window. Uses local-day UTC range query + in-memory overlap (same as
    StaffViewSet.blocked_dates).
    """
    center_tz, block_start, block_end = _block_window(block_date, start_time, end_time, location_id)
    _, day_start, day_end = _center_day_bounds(block_date, location_id)

    day_qs = (
        qs.filter(
            start_time__range=(day_start, day_end),
            status='confirmed',
        )
        .select_related(
            'client',
            'package_purchase',
            'simulator_package_purchase',
            'simulator_credit_redemption',
            'category_asset',
            'coach',
            'simulator',
            'service_category',
        )
    )

    # Overlap: booking_start < block_end AND booking_end > block_start
    return [
        b for b in day_qs
        if b.start_time < block_end and b.end_time > block_start
    ], center_tz, block_start, block_end


def _serialize_booking_preview(booking, center_tz) -> dict:
    client = booking.client
    name = (
        f"{getattr(client, 'first_name', '') or ''} {getattr(client, 'last_name', '') or ''}".strip()
        or getattr(client, 'username', '')
        or getattr(client, 'phone', '')
        or f'Client #{client.id}'
    )

    def _local(aware_dt):
        if not aware_dt:
            return None
        local = aware_dt.astimezone(center_tz)
        return {
            'iso': local.isoformat(),
            'display': local.strftime('%I:%M %p'),
            'date_display': local.strftime('%b %d, %Y'),
        }

    refund_kind = 'none'
    if _is_asset_hours_booking(booking) and booking.package_purchase_id:
        refund_kind = 'category_hours'
    elif booking.booking_type == 'coaching' and booking.package_purchase_id:
        refund_kind = 'session'
    elif booking.booking_type == 'simulator':
        if booking.package_purchase_id and not booking.simulator_credit_redemption_id and not booking.simulator_package_purchase_id:
            refund_kind = 'simulator_hours'
        elif booking.simulator_credit_redemption_id:
            refund_kind = 'simulator_credit_restore'
        else:
            refund_kind = 'simulator_credit_issue'

    return {
        'id': booking.id,
        'booking_type': booking.booking_type,
        'client_id': client.id,
        'client_name': name,
        'client_email': (getattr(client, 'email', None) or '').strip(),
        'client_phone': getattr(client, 'phone', None) or '',
        'start_time': _local(booking.start_time),
        'end_time': _local(booking.end_time),
        'duration_minutes': booking.duration_minutes,
        'coach_name': (
            f"{booking.coach.first_name} {booking.coach.last_name}".strip()
            if booking.coach_id else None
        ),
        'simulator_name': (
            f"Bay {booking.simulator.bay_number} — {booking.simulator.name}"
            if booking.simulator_id else None
        ),
        'asset_name': booking.category_asset.name if booking.category_asset_id else None,
        'will_email': bool((getattr(client, 'email', None) or '').strip()),
        'refund_kind': refund_kind,
    }


def preview_for_staff_block(staff, block_date, start_time, end_time, location_id):
    from bookings.models import Booking

    qs = Booking.objects.filter(coach=staff, booking_type='coaching')
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = f"{staff.first_name} {staff.last_name}".strip() or staff.username
    return {
        'resource_type': 'staff',
        'resource_label': label,
        'count': len(bookings),
        'bookings': [_serialize_booking_preview(b, center_tz) for b in bookings],
        'block_start': block_start.isoformat(),
        'block_end': block_end.isoformat(),
    }


def preview_for_simulator_block(simulator, block_date, start_time, end_time, location_id):
    from bookings.models import Booking

    qs = Booking.objects.filter(simulator=simulator)
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = f"Bay {simulator.bay_number} — {simulator.name}"
    return {
        'resource_type': 'simulator',
        'resource_label': label,
        'count': len(bookings),
        'bookings': [_serialize_booking_preview(b, center_tz) for b in bookings],
        'block_start': block_start.isoformat(),
        'block_end': block_end.isoformat(),
    }


def preview_for_asset_block(asset, block_date, start_time, end_time, location_id):
    from bookings.models import Booking

    qs = Booking.objects.filter(category_asset=asset)
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = asset.name
    return {
        'resource_type': 'asset',
        'resource_label': label,
        'count': len(bookings),
        'bookings': [_serialize_booking_preview(b, center_tz) for b in bookings],
        'block_start': block_start.isoformat(),
        'block_end': block_end.isoformat(),
    }


def _duration_hours(booking) -> Decimal:
    mins = booking.duration_minutes
    if mins is None and booking.start_time and booking.end_time:
        mins = int((booking.end_time - booking.start_time).total_seconds() / 60)
    return Decimal(str(mins or 0)) / Decimal('60')


def _is_asset_hours_booking(booking) -> bool:
    """Prepaid asset-only bookings consume category_hours, not coaching sessions."""
    if not booking.category_asset_id:
        return False
    asset = booking.category_asset
    if asset is not None and not getattr(asset, 'needs_staff', True):
        return True
    # Asset-only create path often has no coach
    if booking.category_asset_id and not booking.coach_id:
        return True
    return False


def refund_booking_credits(booking, issued_by=None) -> dict:
    """
    Restore package sessions / sim hours / category hours / credits for a cancelled booking.
    Mirrors BookingViewSet.cancel restitution, plus category-hour restore.
    """
    from simulators.models import SimulatorCredit

    restitution = {}
    hours = _duration_hours(booking)

    if _is_asset_hours_booking(booking) and booking.package_purchase_id:
        purchase = booking.package_purchase
        purchase.category_hours_remaining = F('category_hours_remaining') + hours
        if purchase.package_status == 'completed':
            purchase.package_status = 'active'
        purchase.save(update_fields=['category_hours_remaining', 'package_status', 'updated_at'])
        purchase.refresh_from_db(fields=['category_hours_remaining', 'package_status'])
        restitution['category_hours_restored'] = float(hours)
        return restitution

    if booking.booking_type == 'coaching' and booking.package_purchase_id:
        purchase = booking.package_purchase
        purchase.sessions_remaining = F('sessions_remaining') + 1
        if purchase.package_status == 'completed':
            purchase.package_status = 'active'
        purchase.save(update_fields=['sessions_remaining', 'package_status', 'updated_at'])
        purchase.refresh_from_db(fields=['sessions_remaining', 'package_status'])
        restitution['sessions_restored'] = 1
        return restitution

    if booking.booking_type == 'simulator':
        # Combo package simulator hours
        if (
            booking.package_purchase_id
            and not booking.simulator_credit_redemption_id
            and not booking.simulator_package_purchase_id
        ):
            purchase = booking.package_purchase
            purchase.simulator_hours_remaining = F('simulator_hours_remaining') + hours
            if purchase.package_status == 'completed':
                purchase.package_status = 'active'
            purchase.save(update_fields=['simulator_hours_remaining', 'package_status', 'updated_at'])
            purchase.refresh_from_db(fields=['simulator_hours_remaining'])
            restitution['simulator_hours_restored'] = float(hours)
            return restitution

        if booking.simulator_package_purchase_id or (
            not booking.package_purchase_id and not booking.simulator_credit_redemption_id
        ):
            credit = SimulatorCredit.objects.create(
                client=booking.client,
                reason=SimulatorCredit.Reason.CANCELLATION,
                hours=hours,
                hours_remaining=hours,
                issued_by=issued_by,
                source_booking=booking,
                notes=f"Credit issued for booking #{booking.id} cancelled by resource block ({hours} hours)",
            )
            restitution['simulator_credit_id'] = credit.id
            restitution['simulator_credit_hours'] = float(hours)
            return restitution

        if booking.simulator_credit_redemption_id:
            credit = booking.simulator_credit_redemption
            credit.hours_remaining = F('hours_remaining') + hours
            credit.status = SimulatorCredit.Status.AVAILABLE
            credit.redeemed_at = None
            credit.save(update_fields=['hours_remaining', 'status', 'redeemed_at'])
            booking.simulator_credit_redemption = None
            booking.save(update_fields=['simulator_credit_redemption'])
            credit.refresh_from_db(fields=['hours_remaining', 'status'])
            restitution['simulator_credit_hours_restored'] = float(hours)
            return restitution

    return restitution


def cancel_overlapping_bookings(
    *,
    bookings,
    reason: str,
    issued_by=None,
    location_id=None,
    resource_label: str = '',
    block_date=None,
    block_start=None,
    block_end=None,
    center_tz=None,
    send_email: bool = True,
) -> dict:
    """
    Cancel bookings, refund credits, optionally email clients.
    `bookings` should already be the overlapping list (not a queryset after materialize).

    Emails are sent AFTER the DB transaction commits so a rollback cannot leave
    clients notified about a cancel that did not stick.
    """
    cancelled_count = 0
    refunded_sessions = 0
    refunded_sim_hours = Decimal('0')
    refunded_cat_hours = Decimal('0')
    emails_sent = 0
    cancelled_ids = []

    with transaction.atomic():
        for booking in bookings:
            booking.status = 'cancelled'
            booking.save(update_fields=['status', 'updated_at'])

            restitution = refund_booking_credits(booking, issued_by=issued_by)
            if restitution.get('sessions_restored'):
                refunded_sessions += restitution['sessions_restored']
            if restitution.get('simulator_hours_restored'):
                refunded_sim_hours += Decimal(str(restitution['simulator_hours_restored']))
            if restitution.get('simulator_credit_hours'):
                refunded_sim_hours += Decimal(str(restitution['simulator_credit_hours']))
            if restitution.get('simulator_credit_hours_restored'):
                refunded_sim_hours += Decimal(str(restitution['simulator_credit_hours_restored']))
            if restitution.get('category_hours_restored'):
                refunded_cat_hours += Decimal(str(restitution['category_hours_restored']))

            cancelled_count += 1
            cancelled_ids.append(booking.id)
            logger.info(
                "Cancelled booking %s for client %s due to block (%s)",
                booking.id,
                getattr(booking.client, 'username', booking.client_id),
                reason,
            )

            try:
                from ghl.tasks import update_user_ghl_custom_fields_task, update_ghl_cancellation_fields_task
                ghl_loc = getattr(booking, 'location_id', None) or location_id
                update_ghl_cancellation_fields_task.delay(booking.client_id, booking_id=booking.id, location_id=ghl_loc)
                update_user_ghl_custom_fields_task.delay(booking.client_id, location_id=ghl_loc)
            except Exception as exc:
                logger.warning("GHL update after block-cancel failed for booking %s: %s", booking.id, exc)

    # Email only after successful commit of the cancel transaction above.
    # When called inside an outer atomic(), pass send_email=False and email after that commits.
    if send_email:
        emails_sent = _email_cancelled_bookings(
            bookings=bookings,
            resource_label=resource_label,
            block_date=block_date,
            block_start=block_start,
            block_end=block_end,
            center_tz=center_tz,
            location_id=location_id,
            reason=reason,
        )

    return {
        'cancelled_bookings': cancelled_count,
        'cancelled_booking_ids': cancelled_ids,
        'refunded_sessions': refunded_sessions,
        'refunded_simulator_hours': float(refunded_sim_hours),
        'refunded_category_hours': float(refunded_cat_hours),
        'emails_sent': emails_sent,
        # extras so callers can email after an outer transaction commits
        '_email_ctx': {
            'bookings': bookings,
            'resource_label': resource_label,
            'block_date': block_date,
            'block_start': block_start,
            'block_end': block_end,
            'center_tz': center_tz,
            'location_id': location_id,
            'reason': reason,
        },
    }


def _email_cancelled_bookings(
    *,
    bookings,
    resource_label='',
    block_date=None,
    block_start=None,
    block_end=None,
    center_tz=None,
    location_id=None,
    reason='',
) -> int:
    sent = 0
    for booking in bookings:
        try:
            from email_service import send_booking_cancelled_by_block_email
            ok = send_booking_cancelled_by_block_email(
                booking=booking,
                resource_label=resource_label,
                block_date=block_date,
                block_start=block_start,
                block_end=block_end,
                center_tz=center_tz,
                location_id=location_id or getattr(booking, 'location_id', None),
                reason=reason,
            )
            if ok:
                sent += 1
        except Exception as exc:
            logger.warning("Cancel email failed for booking %s: %s", booking.id, exc)
    return sent


def finalize_block_cancel_emails(cancel_stats: dict) -> int:
    """Send cancel emails after the outer DB transaction has committed."""
    ctx = (cancel_stats or {}).pop('_email_ctx', None) or {}
    bookings = ctx.get('bookings') or []
    if not bookings:
        return cancel_stats.get('emails_sent', 0) if cancel_stats else 0
    sent = _email_cancelled_bookings(
        bookings=bookings,
        resource_label=ctx.get('resource_label') or '',
        block_date=ctx.get('block_date'),
        block_start=ctx.get('block_start'),
        block_end=ctx.get('block_end'),
        center_tz=ctx.get('center_tz'),
        location_id=ctx.get('location_id'),
        reason=ctx.get('reason') or '',
    )
    if cancel_stats is not None:
        cancel_stats['emails_sent'] = sent
    return sent


def cancel_for_staff_block(staff, block_date, start_time, end_time, location_id, issued_by=None, reason='', send_email=True):
    from bookings.models import Booking

    qs = Booking.objects.filter(coach=staff, booking_type='coaching')
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = f"{staff.first_name} {staff.last_name}".strip() or staff.username
    return cancel_overlapping_bookings(
        bookings=bookings,
        reason=reason or f'Staff blocked: {label}',
        issued_by=issued_by,
        location_id=location_id,
        resource_label=label,
        block_date=block_date,
        block_start=block_start,
        block_end=block_end,
        center_tz=center_tz,
        send_email=send_email,
    )


def cancel_for_simulator_block(simulator, block_date, start_time, end_time, location_id, issued_by=None, reason='', send_email=True):
    from bookings.models import Booking

    qs = Booking.objects.filter(simulator=simulator)
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = f"Bay {simulator.bay_number} — {simulator.name}"
    return cancel_overlapping_bookings(
        bookings=bookings,
        reason=reason or f'Bay blocked: {label}',
        issued_by=issued_by,
        location_id=location_id,
        resource_label=label,
        block_date=block_date,
        block_start=block_start,
        block_end=block_end,
        center_tz=center_tz,
        send_email=send_email,
    )


def cancel_for_asset_block(asset, block_date, start_time, end_time, location_id, issued_by=None, reason='', send_email=True):
    from bookings.models import Booking

    qs = Booking.objects.filter(category_asset=asset)
    bookings, center_tz, block_start, block_end = find_overlapping_bookings(
        qs, block_date, start_time, end_time, location_id
    )
    label = asset.name
    return cancel_overlapping_bookings(
        bookings=bookings,
        reason=reason or f'Asset blocked: {label}',
        issued_by=issued_by,
        location_id=location_id,
        resource_label=label,
        block_date=block_date,
        block_start=block_start,
        block_end=block_end,
        center_tz=center_tz,
        send_email=send_email,
    )
