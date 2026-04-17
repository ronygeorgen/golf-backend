"""
Shared helpers for simulator slot availability and simulator-booking price.
Used by BookingViewSet and by bulk reassignment (simulators app).
"""
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .models import Booking, TempBooking

logger = logging.getLogger(__name__)


def is_simulator_slot_available(
    simulator,
    start_time,
    end_time,
    exclude_booking_id=None,
    use_locking=True,
):
    """
    True if no confirmed/completed booking and no active reserved TempBooking
    overlaps [start_time, end_time) on this simulator.
    """
    booking_query = Booking.objects.filter(
        simulator=simulator,
        start_time__lt=end_time,
        end_time__gt=start_time,
        status__in=['confirmed', 'completed'],
    )
    if exclude_booking_id:
        booking_query = booking_query.exclude(id=exclude_booking_id)
    if use_locking:
        booking_query = booking_query.select_for_update()
    if booking_query.exists():
        return False

    temp_booking_query = TempBooking.objects.filter(
        simulator=simulator,
        start_time__lt=end_time,
        end_time__gt=start_time,
        status='reserved',
        expires_at__gt=timezone.now(),
    )
    if use_locking:
        temp_booking_query = temp_booking_query.select_for_update()
    if temp_booking_query.exists():
        logger.info(
            "Simulator %s blocked by active temp booking for time slot %s - %s",
            getattr(simulator, "bay_number", simulator.pk),
            start_time,
            end_time,
        )
        return False

    return True


def calculate_simulator_booking_price(simulator, duration_minutes):
    """Hourly from simulator or DurationPrice fallback (same rules as BookingViewSet)."""
    if not simulator or not duration_minutes:
        return Decimal('0.00')
    if simulator.is_coaching_bay:
        return Decimal('0.00')
    if simulator.hourly_price:
        hours = Decimal(duration_minutes) / Decimal(60)
        price = (Decimal(simulator.hourly_price) * hours).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        return price
    from simulators.models import DurationPrice

    try:
        duration_price = DurationPrice.objects.get(duration_minutes=duration_minutes)
        return Decimal(duration_price.price)
    except DurationPrice.DoesNotExist:
        return Decimal('0.00')
