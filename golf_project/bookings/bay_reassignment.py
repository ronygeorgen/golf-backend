"""
Bulk reassignment of confirmed future bookings off a simulator bay (e.g. broken bay),
optionally deactivating the bay afterward.
"""
import logging

from django.db import transaction
from django.utils import timezone

from simulators.models import Simulator

from .models import Booking
from .simulator_slot import calculate_simulator_booking_price, is_simulator_slot_available

logger = logging.getLogger(__name__)


def _iter_reassignment_candidates(source_simulator, allow_coaching_bay):
    loc_id = source_simulator.location_id
    qs = (
        Simulator.objects.filter(is_active=True)
        .exclude(pk=source_simulator.pk)
        .order_by('bay_number')
    )
    if loc_id:
        qs = qs.filter(location_id=loc_id)
    candidates = list(qs)
    regular = [s for s in candidates if not s.is_coaching_bay]
    coaching = [s for s in candidates if s.is_coaching_bay]
    yield from regular
    if allow_coaching_bay:
        yield from coaching


def _booking_client_label(booking):
    client = booking.client
    if client is None:
        return ''
    name = (
        f"{getattr(client, 'first_name', '') or ''} {getattr(client, 'last_name', '') or ''}"
    ).strip()
    return (
        name
        or getattr(client, 'username', None)
        or getattr(client, 'phone', None)
        or str(client.pk)
    )


def _serialize_moved(booking, from_sim, to_sim):
    return {
        'booking_id': booking.id,
        'booking_type': booking.booking_type,
        'client_label': _booking_client_label(booking),
        'start_time': booking.start_time.isoformat(),
        'end_time': booking.end_time.isoformat(),
        'from_simulator_id': from_sim.id,
        'from_bay_number': from_sim.bay_number,
        'to_simulator_id': to_sim.id,
        'to_bay_number': to_sim.bay_number,
    }


def _serialize_failed(booking, reason):
    return {
        'booking_id': booking.id,
        'booking_type': booking.booking_type,
        'client_label': _booking_client_label(booking),
        'start_time': booking.start_time.isoformat(),
        'end_time': booking.end_time.isoformat(),
        'reason': reason,
    }


def _affected_bookings_queryset(source_simulator):
    now = timezone.now()
    return (
        Booking.objects.filter(
            simulator=source_simulator,
            status='confirmed',
            start_time__gte=now,
        )
        .select_related('client', 'simulator')
        .order_by('start_time')
    )


def run_deactivate_simulator_reassign(
    source_simulator,
    *,
    dry_run,
    allow_coaching_bay,
    deactivate,
):
    """
    dry_run: if True, only compute moved/failed; no DB writes.
    deactivate: if True, set source_simulator.is_active False after processing bookings (execute only).
    """
    bookings = list(_affected_bookings_queryset(source_simulator))
    moved = []
    failed = []

    if dry_run:
        for booking in bookings:
            target = None
            for sim in _iter_reassignment_candidates(source_simulator, allow_coaching_bay):
                if is_simulator_slot_available(
                    sim,
                    booking.start_time,
                    booking.end_time,
                    exclude_booking_id=booking.id,
                    use_locking=False,
                ):
                    target = sim
                    break
            if target:
                moved.append(_serialize_moved(booking, source_simulator, target))
            else:
                failed.append(
                    _serialize_failed(booking, 'No available bay for this time slot.')
                )
        return {
            'dry_run': True,
            'deactivated': False,
            'moved': moved,
            'failed': failed,
            'source_simulator_id': source_simulator.id,
            'bookings_considered': len(bookings),
        }

    for booking in bookings:
        with transaction.atomic():
            loc_id = source_simulator.location_id
            active_sims_qs = Simulator.objects.filter(is_active=True)
            if loc_id:
                active_sims_qs = active_sims_qs.filter(location_id=loc_id)
            list(active_sims_qs.select_for_update())

            target = None
            for sim in _iter_reassignment_candidates(source_simulator, allow_coaching_bay):
                if is_simulator_slot_available(
                    sim,
                    booking.start_time,
                    booking.end_time,
                    exclude_booking_id=booking.id,
                    use_locking=True,
                ):
                    target = sim
                    break

            if not target:
                failed.append(
                    _serialize_failed(booking, 'No available bay for this time slot.')
                )
                continue

            old_sim = booking.simulator
            booking.simulator = target
            update_fields = ['simulator', 'updated_at']
            if booking.booking_type == 'simulator' and not booking.simulator_credit_redemption_id:
                booking.total_price = calculate_simulator_booking_price(
                    target, booking.duration_minutes
                )
                update_fields.append('total_price')
            booking.save(update_fields=update_fields)
            moved.append(_serialize_moved(booking, old_sim, target))

        try:
            from django.conf import settings
            from ghl.tasks import update_user_ghl_custom_fields_task

            ghl_loc_id = getattr(booking, 'location_id', None) or getattr(
                settings, 'GHL_DEFAULT_LOCATION', None
            )
            update_user_ghl_custom_fields_task.delay(booking.client_id, location_id=ghl_loc_id)
        except Exception as exc:
            logger.warning(
                'GHL custom fields task not queued after bay reassignment: %s', exc
            )

    deactivated = False
    if deactivate and source_simulator.is_active:
        source_simulator.is_active = False
        source_simulator.save(update_fields=['is_active'])
        deactivated = True

    return {
        'dry_run': False,
        'deactivated': deactivated,
        'moved': moved,
        'failed': failed,
        'source_simulator_id': source_simulator.id,
        'bookings_considered': len(bookings),
    }
