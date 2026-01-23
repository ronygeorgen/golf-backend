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
from special_events.models import SpecialEvent, SpecialEventPausedDate  # Ensure all models are imported
from rest_framework.test import APIClient
from django.urls import reverse
from django.utils import timezone

User = get_user_model()

def verify_special_events_conflict():
    print("Testing Special Event overlaps for Guest and Registered users...")
    ts = int(datetime.now().timestamp())
    test_location = f"event_check_{ts}"
    
    # Use tomorrow from 12:00 to 14:00 for the special event
    tomorrow = date.today() + timedelta(days=1)
    event_start_time = dtime(12, 0)
    event_end_time = dtime(14, 0)
    day_of_week = tomorrow.weekday()

    # --- SETUP BAYS ---
    coach_bay = Simulator.objects.create(
        name=f"CoachBay Evt {ts}",
        bay_number=(ts % 500) + 301, # Avoid conflicts with previous test runs
        is_coaching_bay=True,
        is_active=True,
        location_id=test_location,
        redirect_url="http://example.com/pay"
    )
    regular_bay = Simulator.objects.create(
        name=f"RegBay Evt {ts}",
        bay_number=(ts % 500) + 302, # Avoid conflicts
        is_coaching_bay=False,
        is_active=True,
        location_id=test_location,
        hourly_price=Decimal('50.00'),
        redirect_url="http://example.com/pay"
    )
    
    for sim in [coach_bay, regular_bay]:
        SimulatorAvailability.objects.create(
            simulator=sim, day_of_week=day_of_week, 
            start_time=dtime(9, 0), end_time=dtime(22, 0)
        )

    # --- SETUP SPECIAL EVENT ---
    # Create an event that occupies the facility from 12:00 to 14:00
    special_event = SpecialEvent.objects.create(
        title=f"Blocking Event {ts}",
        date=tomorrow,
        start_time=event_start_time,
        end_time=event_end_time,
        event_type="one_time",
        max_capacity=10,
        location_id=test_location,
        is_active=True
    )
    print(f"Created Special Event: {special_event.title} on {tomorrow} {event_start_time}-{event_end_time}")

    # --- SETUP COACH & PACKAGE ---
    coach = User.objects.create_user(
        username=f'evt_coach_{ts}',
        phone=f'+1666{str(ts)[-6:]}1',
        role='staff',
        ghl_location_id=test_location
    )
    StaffAvailability.objects.create(
        staff=coach, day_of_week=day_of_week,
        start_time=dtime(9, 0), end_time=dtime(22, 0)
    )
    tpi_package = CoachingPackage.objects.create(
        title=f"TPI Event Test {ts}",
        description="TPI Test",
        price=Decimal('100.00'),
        session_duration_minutes=60,
        is_active=True,
        is_tpi_assessment=True
    )
    tpi_package.staff_members.add(coach)

    # Helper to create robust purchase
    def create_purchase(user):
        return CoachingPackagePurchase.objects.create(
            client=user, 
            package=tpi_package, 
            sessions_total=5,
            sessions_remaining=5, 
            package_status='active',
            simulator_hours_total=0,
            simulator_hours_remaining=0
        )

    # --- SETUP USERS ---
    # Guest User (created implicitly or via GuestBooking)
    guest_phone = f'+1666{str(ts)[-6:]}2'
    guest_user = User.objects.create_user(username=f'guest_evt_{ts}', phone=guest_phone, ghl_location_id=test_location)
    create_purchase(guest_user)

    # Registered User
    reg_user = User.objects.create_user(username=f'reg_evt_{ts}', phone=f'+1666{str(ts)[-6:]}3', ghl_location_id=test_location)
    create_purchase(reg_user)

    results = {}

    # --- TEST 1: GUEST TPI BOOKING (Coaching Bay) ---
    # Attempt to book 12:30 - 13:30 (inside event window)
    booking_start = timezone.make_aware(datetime.combine(tomorrow, dtime(12, 30)))
    booking_end = booking_start + timedelta(minutes=60)
    
    # Ensure times are ISO formatted properly
    booking_start_str = booking_start.isoformat()
    booking_end_str = booking_end.isoformat()
    
    print("\nAttempting GUEST TPI Booking overlapping with event...")
    client = APIClient()
    data = {
        'booking_type': 'coaching',
        'coaching_package': tpi_package.id,
        'coach': coach.id,
        'start_time': booking_start_str,
        'end_time': booking_end_str,
        'phone': guest_phone,
        'location_id': test_location
    }
    response = client.post(reverse('guest-booking-create'), data, format='json')
    results['guest_tpi'] = response
    print(f"Guest TPI Status: {response.status_code}")
    if response.status_code != 201:
        print(f"Guest TPI Response: {response.content}")

    # --- TEST 2: REGISTERED COACHING BOOKING (Coaching Bay) ---
    print("\nAttempting REGISTERED COACHING Booking overlapping with event...")
    client.force_authenticate(user=reg_user)
    data = {
        'booking_type': 'coaching',
        'coaching_package': tpi_package.id,
        'coach': coach.id,
        'start_time': booking_start_str,
        'end_time': booking_end_str,
        'location_id': test_location
    }
    # Using BookingViewSet create
    response = client.post('/api/bookings/', data, format='json')
    results['reg_coaching'] = response
    print(f"Registered Coaching Status: {response.status_code}")
    if response.status_code != 201:
        print(f"Registered Coaching Response: {response.content}")

    # --- TEST 3: TEMP SIMULATOR BOOKING (Regular Bay) ---
    print("\nAttempting TEMP SIMULATOR Booking overlapping with event...")
    client = APIClient()
    data = {
        'simulator_id': regular_bay.id,
        'buyer_phone': guest_phone, # Reusing guest phone
        'start_time': booking_start_str,
        'end_time': booking_end_str,
        'duration_minutes': 60,
        'total_price': '50.00',
        'location_id': test_location
    }
    response = client.post(reverse('create-temp-booking'), data, format='json')
    results['temp_sim'] = response
    print(f"Temp Sim Status: {response.status_code}")
    if response.status_code != 201:
        print(f"Temp Sim Response: {response.content}")


    # --- VERIFICATION ---
    print("\n--- RESULTS ANALYSIS ---")
    failed = False
    
    # Guest TPI should fail
    if results['guest_tpi'].status_code == 400 and b'conflicts with a special event' in results['guest_tpi'].content:
        print("PASS: Guest TPI blocked by Special Event.")
    else:
        print(f"FAIL: Guest TPI result unexpected. Code: {results['guest_tpi'].status_code}, Body: {results['guest_tpi'].content}")
        failed = True
        
    # Registered Coaching should fail
    if results['reg_coaching'].status_code == 400 and b'conflicts with a special event' in results['reg_coaching'].content:
        print("PASS: Registered Coaching blocked by Special Event.")
    elif results['reg_coaching'].status_code == 400:
        # Check if generic error msg matches expectation or JSON struct
         print(f"PASS (with error): Registered Booking blocked. Body: {results['reg_coaching'].content}")
    else:
        print(f"FAIL: Registered Coaching result unexpected. Code: {results['reg_coaching'].status_code}")
        failed = True

    # Temp Sim should fail
    if results['temp_sim'].status_code == 409 and b'conflicts with a special event' in results['temp_sim'].content:
         print("PASS: Temp Simulator blocked by Special Event.")
    else:
         print(f"FAIL: Temp Simulator result unexpected. Code: {results['temp_sim'].status_code}, Body: {results['temp_sim'].content}")
         failed = True

    if not failed:
        print("\nSUCCESS: All attempts to book overlapping with Special Event were blocked.")
    else:
        print("\nFAILURE: One or more booking types bypassed the Special Event check.")

if __name__ == "__main__":
    verify_special_events_conflict()
