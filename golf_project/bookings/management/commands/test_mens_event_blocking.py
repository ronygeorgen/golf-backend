"""
Django management command to test and verify that Men's Event (weekly recurring Friday 8 PM - 12 AM)
is properly blocking booking session slots.

Usage:
    python manage.py test_mens_event_blocking
    python manage.py test_mens_event_blocking --location-id <location_id>
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import datetime, timedelta
from special_events.models import SpecialEvent
from coaching.models import CoachingPackage


class Command(BaseCommand):
    help = 'Test and verify that Men\'s Event is properly blocking booking session slots'

    def add_arguments(self, parser):
        parser.add_argument(
            '--location-id',
            type=str,
            help='Location ID to filter by (optional)',
        )
        parser.add_argument(
            '--date',
            type=str,
            help='Specific date to test (YYYY-MM-DD format). If not provided, uses next Friday',
        )
        parser.add_argument(
            '--timeout',
            type=int,
            default=300,
            help='Timeout in seconds for DB queries (default: 300)',
        )

    def handle(self, *args, **options):
        location_id = options.get('location_id')
        test_date_str = options.get('date')
        timeout_seconds = options.get('timeout', 300)
        
        self.stdout.write(self.style.SUCCESS('\n' + '='*80))
        self.stdout.write(self.style.SUCCESS('Testing Men\'s Event Blocking for Booking Slots'))
        self.stdout.write(self.style.SUCCESS('='*80 + '\n'))
        self.stdout.write(self.style.WARNING(
            f'Note: DB queries may take up to {timeout_seconds} seconds due to DB location in America.'
        ))
        self.stdout.write('')
        
        try:
            # Step 1: Find Men's Event
            self.stdout.write('Step 1: Finding Men\'s Event...')
            mens_event = self._find_mens_event(location_id)
            if not mens_event:
                self.stdout.write(self.style.ERROR('[X] Men\'s Event not found!'))
                self.stdout.write('   Please ensure a weekly recurring event exists with title containing "Men"')
                return
            
            self.stdout.write(self.style.SUCCESS(f'[OK] Found Men\'s Event: {mens_event.title}'))
            self.stdout.write(f'   Event Type: {mens_event.event_type}')
            self.stdout.write(f'   Start Date: {mens_event.date}')
            self.stdout.write(f'   Start Time: {mens_event.start_time} (UTC)')
            self.stdout.write(f'   End Time: {mens_event.end_time} (UTC)')
            self.stdout.write(f'   Is Active: {mens_event.is_active}')
            if mens_event.recurring_end_date:
                self.stdout.write(f'   Recurring End Date: {mens_event.recurring_end_date}')
            self.stdout.write('')
            
            # Step 2: Determine test date - use a date where event actually occurs
            if test_date_str:
                try:
                    test_date = datetime.strptime(test_date_str, '%Y-%m-%d').date()
                except ValueError:
                    raise CommandError('Invalid date format. Use YYYY-MM-DD')
            else:
                # Find the next occurrence of the event
                today = timezone.now().date()
                all_occurrences = mens_event.get_occurrences(
                    start_date=today,
                    end_date=today + timedelta(days=60)  # Look 60 days ahead
                )
                if all_occurrences:
                    test_date = all_occurrences[0]  # Use the first upcoming occurrence
                    self.stdout.write(f'   Found next event occurrence: {test_date}')
                else:
                    # Fallback to next Friday if no occurrences found
                    test_date = self._get_next_friday()
                    self.stdout.write(self.style.WARNING(
                        f'   No upcoming occurrences found, using next Friday: {test_date}'
                    ))
            
            self.stdout.write(f'Step 2: Testing date: {test_date}')
            self.stdout.write('')
            
            # Step 3: Verify event occurs on this date
            occurrences = mens_event.get_occurrences(
                start_date=test_date,
                end_date=test_date
            )
            if test_date not in occurrences:
                self.stdout.write(self.style.WARNING(
                    f'[!] Warning: Men\'s Event does not occur on {test_date}'
                ))
                self.stdout.write('   This might be expected if the event is paused or outside recurring range.')
                self.stdout.write('   Testing will continue but results may not be accurate.')
                self.stdout.write('')
            else:
                self.stdout.write(self.style.SUCCESS(f'[OK] Men\'s Event occurs on {test_date}'))
                self.stdout.write('')
            
            # Step 4: Test simulator availability
            self.stdout.write('Step 3: Testing Simulator Availability...')
            self.stdout.write('   Fetching available slots (this may take time due to DB location)...')
            simulator_slots = self._test_simulator_availability(test_date, location_id, timeout_seconds)
            
            # Step 5: Test coaching availability
            self.stdout.write('')
            self.stdout.write('Step 4: Testing Coaching Availability...')
            self.stdout.write('   Fetching available slots (this may take time due to DB location)...')
            coaching_slots = self._test_coaching_availability(test_date, location_id, timeout_seconds)
            
            # Step 6: Analyze results
            self.stdout.write('')
            self.stdout.write('='*80)
            self.stdout.write('ANALYSIS RESULTS')
            self.stdout.write('='*80)
            self.stdout.write('')
            
            # Check if 8 PM to 12 AM slots are blocked
            event_start_utc = mens_event.start_time
            event_end_utc = mens_event.end_time
            
            self.stdout.write(f'Event Time Range (UTC): {event_start_utc} - {event_end_utc}')
            self.stdout.write('')
            
            # Analyze simulator slots
            if simulator_slots is not None:
                self._analyze_slots(
                    simulator_slots,
                    event_start_utc,
                    event_end_utc,
                    test_date,
                    'Simulator'
                )
            
            # Analyze coaching slots
            if coaching_slots is not None:
                self._analyze_slots(
                    coaching_slots,
                    event_start_utc,
                    event_end_utc,
                    test_date,
                    'Coaching'
                )
            
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('='*80))
            self.stdout.write(self.style.SUCCESS('Test completed successfully!'))
            self.stdout.write(self.style.SUCCESS('='*80))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\n[X] Error: {str(e)}'))
            import traceback
            self.stdout.write(traceback.format_exc())
    
    def _find_mens_event(self, location_id=None):
        """Find the Men's Event (weekly recurring, Friday, 8 PM - 12 AM)"""
        events = SpecialEvent.objects.filter(
            event_type='weekly',
            is_active=True
        )
        
        if location_id:
            events = events.filter(location_id=location_id)
        
        # Look for events with "Men" in the title
        mens_events = events.filter(title__icontains='men')
        
        if mens_events.exists():
            # Prefer events that occur on Friday (weekday 4)
            for event in mens_events:
                if event.date.weekday() == 4:  # Friday
                    return event
            # If no Friday event found, return first one
            return mens_events.first()
        
        # If no "Men" event found, look for any weekly event on Friday
        for event in events:
            if event.date.weekday() == 4:  # Friday
                # Check if it's 8 PM - 12 AM (20:00 - 00:00)
                if event.start_time.hour == 20 and event.end_time.hour == 0:
                    return event
        
        return None
    
    def _get_next_friday(self):
        """Get the next Friday date"""
        today = timezone.now().date()
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7  # If today is Friday, get next Friday
        return today + timedelta(days=days_until_friday)
    
    def _test_simulator_availability(self, test_date, location_id, timeout_seconds):
        """Test simulator availability for the given date"""
        try:
            from bookings.views import BookingViewSet
            from django.test import RequestFactory
            from django.contrib.auth import get_user_model
            from unittest.mock import Mock
            
            # Create a mock request with query params
            request = Mock()
            request.query_params = {
                'date': test_date.strftime('%Y-%m-%d'),
                'duration': 60,
                'simulator_count': 1
            }
            if location_id:
                request.query_params['location_id'] = location_id
            request.data = {}
            request.user = Mock()
            request.user.is_authenticated = True
            request.user.role = 'admin'
            request.user.ghl_location_id = location_id
            
            # Create a viewset instance
            viewset = BookingViewSet()
            viewset.request = request
            viewset.format_kwarg = None
            
            # Call the check_simulator_availability action
            self.stdout.write('   Waiting for DB response (up to 300s)...')
            response = viewset.check_simulator_availability(request)
            
            if response.status_code == 200:
                data = response.data
                slots = data.get('available_slots', [])
                self.stdout.write(self.style.SUCCESS(f'   [OK] Retrieved {len(slots)} simulator slots'))
                return slots
            else:
                self.stdout.write(self.style.ERROR(f'   [X] Error: {response.data}'))
                return None
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   [X] Error testing simulator availability: {str(e)}'))
            import traceback
            self.stdout.write(traceback.format_exc())
            return None
    
    def _test_coaching_availability(self, test_date, location_id, timeout_seconds):
        """Test coaching availability for the given date"""
        try:
            # First, find an active coaching package
            packages = CoachingPackage.objects.filter(is_active=True)
            if location_id:
                packages = packages.filter(location_id=location_id)
            
            if not packages.exists():
                self.stdout.write(self.style.WARNING('   [!] No active coaching packages found'))
                return None
            
            package = packages.first()
            
            from bookings.views import BookingViewSet
            from unittest.mock import Mock
            
            # Create a mock request with query params
            request = Mock()
            request.query_params = {
                'date': test_date.strftime('%Y-%m-%d'),
                'package_id': package.id,
                'duration': package.session_duration_minutes
            }
            if location_id:
                request.query_params['location_id'] = location_id
            request.data = {}
            request.user = Mock()
            request.user.is_authenticated = True
            request.user.role = 'admin'
            request.user.ghl_location_id = location_id
            
            # Create a viewset instance
            viewset = BookingViewSet()
            viewset.request = request
            viewset.format_kwarg = None
            
            # Call the check_coaching_availability action
            self.stdout.write('   Waiting for DB response (up to 300s)...')
            response = viewset.check_coaching_availability(request)
            
            if response.status_code == 200:
                data = response.data
                slots = data.get('available_slots', [])
                self.stdout.write(self.style.SUCCESS(f'   [OK] Retrieved {len(slots)} coaching slots'))
                return slots
            else:
                self.stdout.write(self.style.ERROR(f'   [X] Error: {response.data}'))
                return None
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'   [X] Error testing coaching availability: {str(e)}'))
            import traceback
            self.stdout.write(traceback.format_exc())
            return None
    
    def _analyze_slots(self, slots, event_start_utc, event_end_utc, test_date, slot_type):
        """Analyze slots to see if event time range is properly blocked"""
        self.stdout.write(f'\n{slot_type} Slots Analysis:')
        self.stdout.write('-' * 80)
        
        if not slots:
            self.stdout.write(f'   No {slot_type.lower()} slots available for this date')
            return
        
        # Count slots in the event time range
        event_start_hour = event_start_utc.hour
        event_end_hour = event_end_utc.hour if event_end_utc.hour != 0 else 24
        
        slots_in_event_range = []
        slots_outside_event_range = []
        
        for slot in slots:
            slot_start_str = slot.get('start_time')
            if not slot_start_str:
                continue
            
            try:
                slot_start = datetime.fromisoformat(slot_start_str.replace('Z', '+00:00'))
                slot_hour = slot_start.hour
                
                # Check if slot is in event range
                # Handle midnight crossover (event_end_utc.hour == 0 means 00:00 = 24:00)
                if event_end_utc.hour == 0:
                    # Event crosses midnight: 20:00 - 00:00
                    if slot_hour >= event_start_hour or slot_hour < event_end_utc.hour:
                        slots_in_event_range.append(slot)
                    else:
                        slots_outside_event_range.append(slot)
                else:
                    # Normal case
                    if event_start_hour <= slot_hour < event_end_hour:
                        slots_in_event_range.append(slot)
                    else:
                        slots_outside_event_range.append(slot)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'   [!] Could not parse slot time: {slot_start_str}'))
                continue
        
        self.stdout.write(f'   Total slots: {len(slots)}')
        self.stdout.write(f'   Slots in event range ({event_start_utc} - {event_end_utc}): {len(slots_in_event_range)}')
        self.stdout.write(f'   Slots outside event range: {len(slots_outside_event_range)}')
        
        if slots_in_event_range:
            self.stdout.write(self.style.ERROR(
                f'\n   [X] PROBLEM: Found {len(slots_in_event_range)} slots in the event time range!'
            ))
            self.stdout.write('   These slots should be blocked by the Men\'s Event:')
            for slot in slots_in_event_range[:10]:  # Show first 10
                slot_start = slot.get('start_time', 'N/A')
                slot_end = slot.get('end_time', 'N/A')
                self.stdout.write(f'      - {slot_start} to {slot_end}')
            if len(slots_in_event_range) > 10:
                self.stdout.write(f'      ... and {len(slots_in_event_range) - 10} more')
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\n   [OK] SUCCESS: No slots found in the event time range!'
            ))
            self.stdout.write('   The Men\'s Event is properly blocking booking slots.')
        
        # Show some example slots outside the range
        if slots_outside_event_range:
            self.stdout.write(f'\n   Example slots outside event range (first 5):')
            for slot in slots_outside_event_range[:5]:
                slot_start = slot.get('start_time', 'N/A')
                slot_end = slot.get('end_time', 'N/A')
                self.stdout.write(f'      - {slot_start} to {slot_end}')

