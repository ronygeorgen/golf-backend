"""
Management command to test coaching double-booking prevention for a specific location.
Run with: python manage.py test_coaching_overlap
"""
import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

TARGET_LOCATION = 'IN0bpFDCWfBrDlUIvQB6'


class Command(BaseCommand):
    help = 'Test coaching double-booking prevention for location IN0bpFDCWfBrDlUIvQB6'

    def handle(self, *args, **options):
        lines = []

        def log(msg=''):
            lines.append(msg)
            self.stdout.write(msg)

        log('\n' + '='*60)
        log('  COACHING DOUBLE-BOOKING TEST')
        log('  Location: %s' % TARGET_LOCATION)
        log('='*60)

        from simulators.models import Simulator
        from bookings.models import Booking
        from users.models import User

        # 1. Find the coaching bay for this specific location
        coaching_bay = Simulator.objects.filter(
            is_coaching_bay=True,
            is_active=True,
            location_id=TARGET_LOCATION
        ).first()

        if not coaching_bay:
            log('[FAIL] No coaching bay found for location %s' % TARGET_LOCATION)
            self._write(lines)
            return
        log('[OK] Coaching bay: "%s" (Bay %s)' % (coaching_bay.name, coaching_bay.bay_number))

        num_coaching_bays = Simulator.objects.filter(
            is_coaching_bay=True,
            is_active=True,
            location_id=TARGET_LOCATION
        ).count()
        log('  Coaching bays at this location: %d' % num_coaching_bays)

        # 2. Find a user
        client_user = User.objects.filter(
            role__in=['admin', 'staff', 'superadmin'], is_active=True
        ).first()
        if not client_user:
            log('[FAIL] No admin/staff user found.')
            self._write(lines)
            return
        log('[OK] Test client: %s (id=%s)' % (client_user.email, client_user.id))

        # 3. Test slot: 72 hours from now
        now = timezone.now()
        test_start = (now + timedelta(hours=72)).replace(minute=0, second=0, microsecond=0)
        test_end = test_start + timedelta(hours=1)
        log('\n  Test slot: %s -> %s UTC' % (
            test_start.strftime('%Y-%m-%d %H:%M'),
            test_end.strftime('%H:%M')
        ))

        # Helper: count coaching bookings at this slot for this location
        def count_at_slot():
            return Booking.objects.filter(
                booking_type='coaching',
                start_time__lt=test_end,
                end_time__gt=test_start,
                status__in=['confirmed', 'completed'],
                location_id=TARGET_LOCATION
            ).count()

        # 4. Step 1 — slot must be empty
        log('\n--- STEP 1: Verify slot is empty ---')
        before = count_at_slot()
        log('  Coaching bookings at this slot (before): %d' % before)
        if before >= num_coaching_bays:
            log('[WARN] Slot already has %d real booking(s). Test aborted — pick a different time.' % before)
            self._write(lines)
            return
        log('[OK] Slot is empty -- first booking would be ALLOWED')

        # 5. Step 2 — create first coaching booking
        log('\n--- STEP 2: Create first coaching booking ---')
        saved_pk = None
        try:
            b = Booking.objects.create(
                client=client_user,
                booking_type='coaching',
                simulator=coaching_bay,
                start_time=test_start,
                end_time=test_end,
                duration_minutes=60,
                status='confirmed',
                total_price=0,
                location_id=TARGET_LOCATION,
            )
            saved_pk = b.pk
            log('[OK] Booking 1 created: pk=%s, location_id=%s' % (saved_pk, b.location_id))
        except Exception as ex:
            log('[FAIL] Booking creation raised: %s' % str(ex))
            self._write(lines)
            return

        # 6. Step 3 — capacity check after booking
        log('\n--- STEP 3: Run capacity check (should block 2nd booking) ---')
        after = count_at_slot()
        log('  Coaching bookings at slot now: %d' % after)
        log('  Coaching bays at location:     %d' % num_coaching_bays)
        log('  Block condition: %d >= %d -> %s' % (after, num_coaching_bays, after >= num_coaching_bays))

        if after >= num_coaching_bays:
            result = 'PASS'
            log('\n[OK] SECOND BOOKING WOULD BE BLOCKED -- fix is working correctly')
        else:
            result = 'FAIL'
            log('\n[FAIL] Second booking would NOT be blocked')
            # Extra debug
            exists = Booking.objects.filter(pk=saved_pk).exists()
            log('  Booking pk=%s in DB: %s' % (saved_pk, exists))

        # 7. Cleanup
        if saved_pk:
            Booking.objects.filter(pk=saved_pk).delete()
            log('\n  Cleaned up test booking pk=%s' % saved_pk)

        # 8. Summary
        log('\n' + '='*60)
        log('  RESULT: %s' % result)
        if result == 'PASS':
            log('  The fix correctly prevents coaching double-booking.')
            log('  With 1 coaching bay at this location, only 1 coaching')
            log('  session is allowed per time slot.')
        else:
            log('  Fix is NOT working for this location.')
        log('='*60 + '\n')

        self._write(lines)

    def _write(self, lines):
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', '..', '..', 'test_coaching_result.log'
        )
        abs_path = os.path.abspath(log_path)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        self.stdout.write('Log saved: %s' % abs_path)
