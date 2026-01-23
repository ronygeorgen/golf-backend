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
from coaching.models import CoachingPackage, CoachingPackagePurchase
from simulators.models import Simulator, SimulatorAvailability
from bookings.models import Booking
from users.models import StaffAvailability
from special_events.models import SpecialEvent
from rest_framework.test import APIClient
from django.urls import reverse
from django.utils import timezone

User = get_user_model()

def verify_next_day_utc_conflict():
    print("Testing Next Day UTC Conflict (Late Night Slot)...")
    ts = int(datetime.now().timestamp())
    test_location = f"utc_check_{ts}"
    
    # Use tomorrow as the booking date
    booking_date = date.today() + timedelta(days=1)
    booking_date_str = booking_date.isoformat()
    
    # Setup NEXT DAY as the event date
    # Scenario: 
    # User books Friday (Jan 23rd).
    # Slot is Friday 8 PM AST -> Saturday 00:00 UTC.
    # Event is on Saturday (Jan 24th) 00:00 UTC.
    # The Availability Check for Jan 23rd MUST find the event on Jan 24th.
    
    event_date = booking_date + timedelta(days=1)
    
    # Event starts at 00:00 (Midnight)
    event_start = dtime(0, 0)
    event_end = dtime(4, 0)
    
    day_of_week = booking_date.weekday()

    # --- SETUP BAYS ---
    # Need availabilty late into the night (e.g. up to 04:00 next day) to generate late slots
    # or simply use standard availability.
    # 8 PM AST is 00:00 UTC.
    # If the system uses UTC for availability, we need the bay to be open at 00:00 UTC.
    # Let's just open the bay 24/7 for simplicity
    coach_bay = Simulator.objects.create(
        name=f"CoachBay UTC {ts}",
        bay_number=701,
        is_coaching_bay=True,
        is_active=True,
        location_id=test_location,
        redirect_url="http://example.com/pay"
    )
    SimulatorAvailability.objects.create(
        simulator=coach_bay, day_of_week=day_of_week, 
        start_time=dtime(0, 0), end_time=dtime(23, 59)
    )

    # --- SETUP SPECIAL EVENT (Next Day) ---
    special_event = SpecialEvent.objects.create(
        title=f"Late Night Event {ts}",
        date=event_date,
        start_time=event_start,
        end_time=event_end,
        event_type="one_time",
        max_capacity=10,
        location_id=test_location,
        is_active=True
    )
    print(f"Created Event: {special_event.title} on {event_date} {event_start}-{event_end}")

    # --- TEST API AVAILABILITY ---
    client = APIClient()
    url = reverse('booking-check-simulator-availability')
    
    print(f"Checking availability for date: {booking_date_str}")
    response = client.get(url, {
        'date': booking_date_str,
        'duration': 60,
        'simulator_count': 1,
        'location_id': test_location
    })
    
    if response.status_code == 200:
        slots = response.data.get('available_slots', [])
        # We are looking for slots that would overlap with the event (00:00 on event_date)
        # e.g. a slot starting at 00:00 on event_date
        
        # Convert event start to string for matching
        target_slot_start = timezone.make_aware(datetime.combine(event_date, event_start)).isoformat()
        
        found_conflict_slot = False
        for slot in slots:
            if slot['start_time'] == target_slot_start:
                found_conflict_slot = True
                print(f"FAIL: Found slot overlapping with next-day event! {slot['start_time']}")
                
        if not found_conflict_slot:
            print("PASS: No slots found overlapping with next-day event.")
    else:
        print(f"Error checking availability: {response.status_code}")

if __name__ == "__main__":
    verify_next_day_utc_conflict()
