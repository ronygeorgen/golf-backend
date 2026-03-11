from django.core.management.base import BaseCommand
from bookings.models import Booking
from special_events.models import SpecialEvent
from users.models import StaffAvailability, StaffDayAvailability
from simulators.models import SimulatorAvailability
from admin_panel.models import ClosedDay

class Command(BaseCommand):
    help = 'Exports database time values to a text file for review'

    def handle(self, *args, **kwargs):
        output_file = 'e:/Saasyway/Adam-Wheeler/Golf-New-Project/db_times_review.txt'
        with open(output_file, 'w') as f:
            f.write("=== DATABASE TIMES REVIEW ===\n")
            f.write("This file shows how exactly the data is currently stored in the database models.\n")
            f.write("Note: Django stores DateTimeField in UTC when USE_TZ=True, but TimeField and DateField are typically naïve.\n\n")

            f.write("--- 1. Bookings (Up to 10) ---\n")
            for b in Booking.objects.order_by('-id')[:10]:
                f.write(f"ID: {b.id} | Client: {b.client.username if b.client else 'N/A'}\n")
                f.write(f"  start_time: {repr(b.start_time)}\n")
                f.write(f"  end_time:   {repr(b.end_time)}\n")
                f.write(f"  created_at: {repr(b.created_at)}\n\n")

            f.write("--- 2. Special Events (Up to 10) ---\n")
            for e in SpecialEvent.objects.order_by('-id')[:10]:
                f.write(f"ID: {e.id} | Title: {e.title} | Type: {e.event_type}\n")
                f.write(f"  date:               {repr(e.date)}\n")
                f.write(f"  recurring_end_date: {repr(e.recurring_end_date)}\n")
                f.write(f"  start_time:         {repr(e.start_time)}\n")
                f.write(f"  end_time:           {repr(e.end_time)}\n\n")

            f.write("--- 3. Staff Availability (Up to 10) ---\n")
            for sa in StaffAvailability.objects.all()[:10]:
                f.write(f"ID: {sa.id} | Staff: {sa.staff.username if sa.staff else 'N/A'} | Day_of_week: {sa.day_of_week}\n")
                f.write(f"  start_time: {repr(sa.start_time)}\n")
                f.write(f"  end_time:   {repr(sa.end_time)}\n\n")

            f.write("--- 4. Staff Day Availability (Up to 10) ---\n")
            for sda in StaffDayAvailability.objects.order_by('-id')[:10]:
                f.write(f"ID: {sda.id} | Staff: {sda.staff.username if sda.staff else 'N/A'}\n")
                f.write(f"  date:       {repr(sda.date)}\n")
                f.write(f"  start_time: {repr(sda.start_time)}\n")
                f.write(f"  end_time:   {repr(sda.end_time)}\n\n")

            f.write("--- 5. Simulator Availability (Up to 10) ---\n")
            for sim_a in SimulatorAvailability.objects.all()[:10]:
                f.write(f"ID: {sim_a.id} | Simulator: {sim_a.simulator.name if sim_a.simulator else 'N/A'} | Day_of_week: {sim_a.day_of_week}\n")
                f.write(f"  start_time: {repr(sim_a.start_time)}\n")
                f.write(f"  end_time:   {repr(sim_a.end_time)}\n\n")

            f.write("--- 6. Closed Days (Up to 10) ---\n")
            for cd in ClosedDay.objects.order_by('-id')[:10]:
                f.write(f"ID: {cd.id} | Title: {cd.title} | Recurrence: {cd.recurrence}\n")
                f.write(f"  start_date: {repr(cd.start_date)}\n")
                f.write(f"  end_date:   {repr(cd.end_date)}\n")
                f.write(f"  start_time: {repr(cd.start_time)}\n")
                f.write(f"  end_time:   {repr(cd.end_time)}\n\n")

        self.stdout.write(self.style.SUCCESS(f"Successfully exported to {output_file}"))
