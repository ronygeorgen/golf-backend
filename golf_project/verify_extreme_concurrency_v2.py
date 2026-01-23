import os
import django
import threading
import uuid
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
from bookings.models import Booking, TempBooking
from users.models import StaffAvailability
from rest_framework.test import APIClient
from django.urls import reverse
from django.utils import timezone

User = get_user_model()

def run_extreme_concurrency_test():
    print("Testing EXTREME Concurrency: TPI Guest, Registered Guest, Rescheduling, and Fallback...")
    ts = int(datetime.now().timestamp())
    test_location = f"extreme_v2_{ts}"
    
    tomorrow = date.today() + timedelta(days=2) # Further in future
    start_dt = timezone.make_aware(datetime.combine(tomorrow, dtime(11, 0)))
    end_dt = start_dt + timedelta(hours=1)
    day_of_week = tomorrow.weekday()

    # --- SETUP BAYS ---
    coach_bay = Simulator.objects.create(
        name=f"Coach Bay {ts}",
        bay_number=101,
        is_coaching_bay=True,
        is_active=True,
        location_id=test_location,
        redirect_url="http://example.com/pay"
    )
    regular_bay = Simulator.objects.create(
        name=f"Reg Bay {ts}",
        bay_number=102,
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

    # --- SETUP COACH & TPI PACKAGE ---
    coach = User.objects.create_user(
        username=f'ex_v2_coach_{ts}',
        phone=f'+1444{str(ts)[-6:]}1',
        role='staff',
        ghl_location_id=test_location
    )
    StaffAvailability.objects.create(
        staff=coach, day_of_week=day_of_week,
        start_time=dtime(9, 0), end_time=dtime(22, 0)
    )
    tpi_package = CoachingPackage.objects.create(
        title=f"TPI EX V2 {ts}",
        description="TPI Test",
        price=Decimal('100.00'),
        session_duration_minutes=60,
        is_active=True,
        is_tpi_assessment=True
    )
    tpi_package.staff_members.add(coach)

    def create_pur(user):
        return CoachingPackagePurchase.objects.create(
            client=user, package=tpi_package, sessions_total=5, sessions_remaining=5, 
            package_status='active', simulator_hours_total=0, simulator_hours_remaining=0
        )

    # --- SETUP USERS ---
    user1 = User.objects.create_user(username=f'ex2_u1_{ts}', phone=f'+1444{str(ts)[-6:]}2', ghl_location_id=test_location)
    create_pur(user1)

    user2 = User.objects.create_user(username=f'ex2_u2_{ts}', phone=f'+1444{str(ts)[-6:]}3', ghl_location_id=test_location)
    create_pur(user2)
    
    old_start = start_dt - timedelta(hours=5)
    booking_to_resched = Booking.objects.create(
        client=user2, coach=coach, coaching_package=tpi_package, 
        start_time=old_start, end_time=old_start + timedelta(hours=1), 
        booking_type='coaching', location_id=test_location, total_price=Decimal('100.00')
    )

    user3_phone = f'+1444{str(ts)[-6:]}4'
    user4 = User.objects.create_user(username=f'ex2_u4_{ts}', phone=f'+1444{str(ts)[-6:]}5', ghl_location_id=test_location)
    create_pur(user4)

    # --- TEST TASKS ---
    results = []
    barrier = threading.Barrier(4)

    def guest_tpi_task():
        client = APIClient()
        data = {'booking_type': 'coaching', 'coaching_package': tpi_package.id, 'coach': coach.id, 
                'start_time': start_dt.isoformat(), 'end_time': end_dt.isoformat(), 
                'phone': user1.phone, 'location_id': test_location}
        barrier.wait()
        r = client.post(reverse('guest-booking-create'), data, format='json')
        results.append(('GUEST_TPI', r.status_code, r.content))

    def resched_task():
        client = APIClient()
        client.force_authenticate(user=user2)
        data = {'start_time': start_dt.isoformat(), 'end_time': end_dt.isoformat(), 'coach': coach.id}
        barrier.wait()
        r = client.post(reverse('booking-reschedule', args=[booking_to_resched.id]), data, format='json')
        results.append(('RESCHEDULE', r.status_code, r.content))

    def guest_sim_task():
        client = APIClient()
        data = {'simulator_id': regular_bay.id, 'buyer_phone': user3_phone, 
                'start_time': start_dt.isoformat(), 'end_time': end_dt.isoformat(), 
                'duration_minutes': 60, 'total_price': '50.00', 'location_id': test_location}
        barrier.wait()
        r = client.post(reverse('create-temp-booking'), data, format='json')
        results.append(('TEMP_SIM', r.status_code, r.content))

    def fallback_tpi_task():
        client = APIClient()
        data = {'booking_type': 'coaching', 'coaching_package': tpi_package.id, 'coach': coach.id, 
                'start_time': start_dt.isoformat(), 'end_time': end_dt.isoformat(), 
                'phone': user4.phone, 'location_id': test_location}
        barrier.wait()
        r = client.post(reverse('guest-booking-create'), data, format='json')
        results.append(('FALLBACK_TPI', r.status_code, r.content))

    threads = [threading.Thread(target=guest_tpi_task), threading.Thread(target=resched_task),
               threading.Thread(target=guest_sim_task), threading.Thread(target=fallback_tpi_task)]
    for t in threads: t.start()
    for t in threads: t.join()

    # --- VERIFICATION ---
    print("\n--- TEST RESULTS ---")
    bay_occupancy = {coach_bay.id: [], regular_bay.id: []}
    
    for label, status, content in results:
        print(f"{label}: Status {status}")
        if status in [200, 201]:
            data = json.loads(content)
            # Find bay ID
            bay_id = None
            if 'booking' in data: # Guest or Resched
                bay_id = data['booking'].get('simulator')
            elif 'temp_id' in data: # TempBooking
                # For temp booking, it returns the requested simulator_id (regular_bay in our test)
                bay_id = regular_bay.id
            
            if bay_id:
                bay_occupancy.setdefault(bay_id, []).append(label)
        else:
            print(f"  Rejected: {content[:100]}...")

    print("\n--- OCCUPANCY CHECK ---")
    failed = False
    for b_id, labels in bay_occupancy.items():
        bay = Simulator.objects.get(id=b_id)
        print(f"Simulator {bay.name} (ID {bay.id}): Occupied by {labels}")
        if len(labels) > 1:
            print(f"  !!! ERROR: Double booking on {bay.name} !!!")
            failed = True

    if not failed:
        print("\nPASSED: No double-bookings! Valid blocking handled concurrent requests.")
    else:
        print("\nFAILED: Double booking detected!")

if __name__ == "__main__":
    run_extreme_concurrency_test()
