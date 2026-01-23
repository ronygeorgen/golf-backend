import os
import django
import threading
import uuid
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
    test_location = f"extreme_{ts}"
    
    # Use tomorrow at 20:00
    tomorrow = date.today() + timedelta(days=1)
    start_dt = timezone.make_aware(datetime.combine(tomorrow, dtime(20, 0)))
    end_dt = start_dt + timedelta(hours=1)
    day_of_week = tomorrow.weekday()

    # --- SETUP BAYS ---
    coach_bay = Simulator.objects.create(
        name=f"Coaching Bay {ts}",
        bay_number=(ts % 500) + 100,
        is_coaching_bay=True,
        is_active=True,
        location_id=test_location,
        redirect_url="http://example.com/pay"
    )
    regular_bay = Simulator.objects.create(
        name=f"Regular Bay {ts}",
        bay_number=(ts % 500) + 200,
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
        username=f'extreme_coach_{ts}',
        phone=f'+1555{str(ts)[-6:]}1',
        role='staff',
        ghl_location_id=test_location
    )
    StaffAvailability.objects.create(
        staff=coach, day_of_week=day_of_week,
        start_time=dtime(9, 0), end_time=dtime(22, 0)
    )
    tpi_package = CoachingPackage.objects.create(
        title=f"TPI Package {ts}",
        description="TPI Test",
        price=Decimal('100.00'),
        session_duration_minutes=60,
        is_active=True,
        is_tpi_assessment=True
    )
    tpi_package.staff_members.add(coach)

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
    phone1 = f'+1555{str(ts)[-6:]}2'
    user1 = User.objects.create_user(username=f'u1_{ts}', phone=phone1, ghl_location_id=test_location)
    create_purchase(user1)

    phone2 = f'+1555{str(ts)[-6:]}3'
    user2 = User.objects.create_user(username=f'u2_{ts}', phone=phone2, ghl_location_id=test_location)
    create_purchase(user2)
    
    old_start = start_dt - timedelta(hours=3)
    old_end = old_start + timedelta(hours=1)
    booking_to_resched = Booking.objects.create(
        client=user2, coach=coach, coaching_package=tpi_package, 
        start_time=old_start, end_time=old_end, booking_type='coaching', 
        location_id=test_location, total_price=Decimal('100.00')
    )

    phone3 = f'+1555{str(ts)[-6:]}4'
    user3 = User.objects.create_user(username=f'u3_{ts}', phone=phone3, ghl_location_id=test_location)

    phone4 = f'+1555{str(ts)[-6:]}5'
    user4 = User.objects.create_user(username=f'u4_{ts}', phone=phone4, ghl_location_id=test_location)
    create_purchase(user4)

    # --- TEST TASKS ---
    results = []
    barrier = threading.Barrier(4)

    def guest_tpi_task():
        client = APIClient()
        data = {
            'booking_type': 'coaching',
            'coaching_package': tpi_package.id,
            'coach': coach.id,
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat(),
            'phone': phone1,
            'location_id': test_location
        }
        barrier.wait()
        url = reverse('guest-booking-create')
        response = client.post(url, data, format='json')
        results.append(('GUEST_TPI', response.status_code, response.content))

    def resched_task():
        client = APIClient()
        client.force_authenticate(user=user2)
        data = {
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat(),
            'coach': coach.id
        }
        barrier.wait()
        url = reverse('booking-reschedule', args=[booking_to_resched.id])
        response = client.post(url, data, format='json')
        results.append(('RESCHEDULE', response.status_code, response.content))

    def guest_sim_task():
        client = APIClient()
        data = {
            'simulator_id': regular_bay.id,
            'buyer_phone': phone3,
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat(),
            'duration_minutes': 60,
            'total_price': '50.00',
            'location_id': test_location
        }
        barrier.wait()
        url = reverse('create-temp-booking')
        response = client.post(url, data, format='json')
        results.append(('TEMP_SIM', response.status_code, response.content))

    def fallback_tpi_task():
        client = APIClient()
        data = {
            'booking_type': 'coaching',
            'coaching_package': tpi_package.id,
            'coach': coach.id,
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat(),
            'phone': phone4,
            'location_id': test_location
        }
        barrier.wait()
        url = reverse('guest-booking-create')
        response = client.post(url, data, format='json')
        results.append(('FALLBACK_TPI', response.status_code, response.content))

    threads = [
        threading.Thread(target=guest_tpi_task),
        threading.Thread(target=resched_task),
        threading.Thread(target=guest_sim_task),
        threading.Thread(target=fallback_tpi_task)
    ]
    
    for t in threads: t.start()
    for t in threads: t.join()

    # --- VERIFICATION ---
    print("\n--- TEST RESULTS ---")
    success_map = {
        'GUEST_TPI': False, 'RESCHEDULE': False, 
        'TEMP_SIM': False, 'FALLBACK_TPI': False
    }
    
    for label, status, content in results:
        print(f"{label}: Status {status}")
        if status in [200, 201]:
            success_map[label] = True
        else:
            print(f"  Rejected: {content[:150]}...")

    print(f"\nFinal Success Matrix: {success_map}")
    
    coaching_bay_occupants = (1 if success_map['GUEST_TPI'] else 0) + (1 if success_map['RESCHEDULE'] else 0)
    regular_bay_occupants = (1 if success_map['TEMP_SIM'] else 0) + (1 if success_map['FALLBACK_TPI'] else 0)

    print(f"Coaching Bay successes: {coaching_bay_occupants}")
    print(f"Regular Bay successes: {regular_bay_occupants}")

    if coaching_bay_occupants <= 1 and regular_bay_occupants <= 1:
        print("\nPASSED: Each bay was only booked ONCE. No double booking!")
    else:
        print("\nFAILED: DOUBLE BOOKING DETECTED!")

if __name__ == "__main__":
    run_extreme_concurrency_test()
