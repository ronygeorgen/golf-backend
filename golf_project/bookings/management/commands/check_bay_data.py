"""
Diagnostic: shows coaching bays grouped by location.
Run with: python manage.py check_bay_data
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Show coaching bays per location'

    def handle(self, *args, **options):
        from simulators.models import Simulator

        self.stdout.write('\n=== COACHING BAYS PER LOCATION ===\n')

        coaching_bays = Simulator.objects.filter(is_coaching_bay=True, is_active=True).order_by('location_id', 'bay_number')

        by_location = {}
        for bay in coaching_bays:
            loc = bay.location_id or '(None - unset)'
            if loc not in by_location:
                by_location[loc] = []
            by_location[loc].append(bay)

        for loc, bays in by_location.items():
            self.stdout.write('\nLocation: %s  (%d coaching bay(s))' % (loc, len(bays)))
            for bay in bays:
                self.stdout.write('  Bay %-4s | %s' % (bay.bay_number, bay.name))

        self.stdout.write('\n--- SUMMARY ---')
        self.stdout.write('  Total locations with coaching bays: %d' % len(by_location))
        self.stdout.write('  Max coaching bays in one location: %d' % (max(len(v) for v in by_location.values()) if by_location else 0))
        self.stdout.write('\nThe fix blocks a new coaching booking when:')
        self.stdout.write('  (existing coaching bookings at this time) >= (coaching bays at this location)\n')
