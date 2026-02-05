from django.core.management.base import BaseCommand
from users.models import StaffAvailability, StaffDayAvailability
from datetime import datetime
import pytz

class Command(BaseCommand):
    help = 'Convert staff availability times from UTC to Halifax local time'

    def handle(self, *args, **options):
        utc = pytz.UTC
        halifax = pytz.timezone('America/Halifax')
        ref_date = datetime(2026, 2, 1)  # Winter date for EST conversion
        
        self.stdout.write(self.style.WARNING('\n🔄 Starting migration...\n'))
        
        # Migrate StaffAvailability (recurring weekly)
        recurring_count = 0
        recurring_availabilities = StaffAvailability.objects.all()
        
        if recurring_availabilities.count() == 0:
            self.stdout.write(self.style.WARNING('No recurring availability records found.'))
        else:
            for avail in recurring_availabilities:
                # Current times are stored as UTC
                utc_start = datetime.combine(ref_date, avail.start_time)
                utc_start = utc.localize(utc_start)
                
                utc_end = datetime.combine(ref_date, avail.end_time)
                utc_end = utc.localize(utc_end)
                
                # Convert to Halifax timezone
                halifax_start = utc_start.astimezone(halifax)
                halifax_end = utc_end.astimezone(halifax)
                
                # Update with Halifax local times
                old_start = avail.start_time
                old_end = avail.end_time
                
                avail.start_time = halifax_start.time()
                avail.end_time = halifax_end.time()
                avail.save()
                
                recurring_count += 1
                self.stdout.write(
                    f"  ✓ {avail.staff.username} - {avail.get_day_of_week_display()}: "
                    f"{old_start} UTC → {avail.start_time} Halifax"
                )
        
        # Migrate StaffDayAvailability (day-specific)
        day_count = 0
        day_availabilities = StaffDayAvailability.objects.all()
        
        if day_availabilities.count() == 0:
            self.stdout.write(self.style.WARNING('\nNo day-specific availability records found.'))
        else:
            self.stdout.write('\n')
            for avail in day_availabilities:
                utc_start = datetime.combine(ref_date, avail.start_time)
                utc_start = utc.localize(utc_start)
                
                utc_end = datetime.combine(ref_date, avail.end_time)
                utc_end = utc.localize(utc_end)
                
                halifax_start = utc_start.astimezone(halifax)
                halifax_end = utc_end.astimezone(halifax)
                
                old_start = avail.start_time
                old_end = avail.end_time
                
                avail.start_time = halifax_start.time()
                avail.end_time = halifax_end.time()
                avail.save()
                
                day_count += 1
                self.stdout.write(
                    f"  ✓ {avail.staff.username} - {avail.date}: "
                    f"{old_start} UTC → {avail.start_time} Halifax"
                )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'\n✅ Migration complete!\n'
                f'   - Recurring availability: {recurring_count} records\n'
                f'   - Day-specific availability: {day_count} records\n'
            )
        )
