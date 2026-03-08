"""
Check if bookings are stored with correct UTC (DST-aware) or might be shifted.

Run: python manage.py check_booking_timezone [--limit 10] [--location-id ID]

Output shows each booking's:
- Stored UTC
- Display in Halifax (America/Halifax) - what customers see now
- "If stored as AST" - what it would show if stored with fixed UTC-4
- "If stored as ADT" - what it would show if stored with fixed UTC-3

If the Halifax column shows sensible business hours (e.g. 9am-6pm) and the
AST/ADT columns look wrong, your storage is likely correct. If Halifax times
look 1hr off from expected, you may have had a fixed-offset bug.
"""
from django.core.management.base import BaseCommand
from bookings.models import Booking
from golf_project.timezone_utils import utc_to_local
from datetime import timedelta


def fmt(dt):
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, 'strftime') else str(dt)


class Command(BaseCommand):
    help = "Check booking times: UTC vs Halifax local (what customers see)"

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=15)
        parser.add_argument('--location-id', type=str, default=None)

    def handle(self, *args, **options):
        limit = options['limit']
        location_id = options.get('location_id')

        qs = Booking.objects.filter(status__in=['confirmed', 'completed']).order_by('-start_time')
        if location_id:
            qs = qs.filter(location_id=location_id)
        bookings = list(qs[:limit])

        if not bookings:
            self.stdout.write("No confirmed/completed bookings found.")
            return

        self.stdout.write(f"\n{'='*80}")
        self.stdout.write("BOOKING TIME CHECK (UTC vs Halifax local = what customers see)")
        self.stdout.write(f"{'='*80}\n")

        for b in bookings:
            start_utc = b.start_time
            end_utc = b.end_time

            # What frontend shows (correct DST)
            start_local = utc_to_local(start_utc, location_id or b.location_id)
            end_local = utc_to_local(end_utc, location_id or b.location_id)

            # If stored with wrong fixed offset (for comparison)
            # AST = UTC-4: local = utc + 4h
            start_if_ast = start_utc + timedelta(hours=4) if start_utc else None
            end_if_ast = end_utc + timedelta(hours=4) if end_utc else None
            # ADT = UTC-3: local = utc + 3h
            start_if_adt = start_utc + timedelta(hours=3) if start_utc else None
            end_if_adt = end_utc + timedelta(hours=3) if end_utc else None

            client_name = f"{b.client.first_name or ''} {b.client.last_name or ''}".strip() or b.client.username
            self.stdout.write(f"ID {b.id} | {b.booking_type} | {client_name}")
            self.stdout.write(f"  Stored UTC:       {fmt(start_utc)} - {fmt(end_utc)}")
            self.stdout.write(f"  Halifax (now):    {fmt(start_local)} - {fmt(end_local)}  <-- what customers see")
            self.stdout.write(f"  If fixed UTC-4:   {fmt(start_if_ast)} - {fmt(end_if_ast)}")
            self.stdout.write(f"  If fixed UTC-3:   {fmt(start_if_adt)} - {fmt(end_if_adt)}")
            self.stdout.write("")

        self.stdout.write(f"\nInterpretation:")
        self.stdout.write("- Halifax (now) = correct DST conversion. Times should be in normal business hours (e.g. 8am-8pm).")
        self.stdout.write("- If Halifax times look odd (e.g. 7am or 9pm for midday slots), old storage may have used wrong offset.")
        self.stdout.write("- 1-hour shift from expected = likely fixed AST or ADT used year-round.")
