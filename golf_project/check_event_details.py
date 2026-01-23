import os
import django
import json
from datetime import datetime, time as dtime, timedelta, date
from decimal import Decimal

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'golf_project.settings')
django.setup()

from django.conf import settings
settings.ALLOWED_HOSTS = ['*']

from django.contrib.auth import get_user_model
from special_events.models import SpecialEvent
from django.utils import timezone

def check_event_details():
    print("Checking Special Events for today and upcoming...")
    today = date.today()
    
    events = SpecialEvent.objects.filter(is_active=True, date__gte=today)
    
    found = False
    for event in events:
        found = True
        print(f"\nEvent: {event.title} (ID: {event.id})")
        print(f"  Type: {event.event_type}")
        print(f"  Date: {event.date}")
        print(f"  Time: {event.start_time} - {event.end_time}")
        print(f"  Location ID: {event.location_id}")
        
        # Check occurrences
        occs = event.get_occurrences(start_date=today, end_date=today + timedelta(days=7))
        print(f"  Occurrences next 7 days: {occs}")
        
        # Check specific conflict
        # User says "8pm tonight from 8-9pm" (20:00 - 21:00)
        # Assuming "tonight" means today's date
        booking_start = timezone.make_aware(datetime.combine(today, dtime(20, 0)))
        booking_end = booking_start + timedelta(minutes=60)
        
        is_conflict = event.conflicts_with_range(booking_start, booking_end)
        print(f"  Conflicts with today 20:00-21:00? {is_conflict}")
        
        if event.start_time == dtime(20, 0):
             print("  -> Starts exactly at 20:00")
        
        # Debug conflict logic details
        start_date = booking_start.date()
        end_date = booking_end.date()
        print(f"  Debug: Checking range {booking_start} to {booking_end}")
        
        occurrences = event.get_occurrences(start_date=start_date, end_date=end_date)
        for occ_date in occurrences:
            event_start_dt = timezone.make_aware(datetime.combine(occ_date, event.start_time))
            event_end_dt = timezone.make_aware(datetime.combine(occ_date, event.end_time))
            if event.end_time < event.start_time:
                event_end_dt += timedelta(days=1)
                
            print(f"    Occurrence {occ_date}: {event_start_dt} to {event_end_dt}")
            
            # Standard overlap logic: StartA < EndB and EndA > StartB
            cond1 = booking_start < event_end_dt
            cond2 = booking_end > event_start_dt
            print(f"    Overlap check: ({booking_start} < {event_end_dt}) and ({booking_end} > {event_start_dt})")
            print(f"    {cond1} and {cond2} = {cond1 and cond2}")


    if not found:
        print("No upcoming active events found.")

if __name__ == "__main__":
    check_event_details()
