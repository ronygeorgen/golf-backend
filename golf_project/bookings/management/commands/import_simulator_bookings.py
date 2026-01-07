"""
Django management command to import simulator session bookings from CSV file.

This command reads a CSV file with booking data and creates:
1. Booking records for simulator sessions
2. Links bookings to package purchases (combo or simulator-only)
3. Consumes hours from packages (reduces hours_remaining)

CSV Format Expected:
- customer_identifier: Phone number (e.g., (902) 483-5479 or 9024835479)
- start_time: DateTime in MM-DD-YYYY HH:MM format (e.g., 07-01-2026 10:00)
- end_time: DateTime in MM-DD-YYYY HH:MM format (e.g., 07-01-2026 11:00)
- duration_minutes: Duration in minutes (e.g., 60)
- simulator_bay_number: Optional bay number (will auto-assign if not provided)
- status: Booking status (confirmed, completed, cancelled, no_show) - defaults to 'confirmed'
- location_id: Optional GHL location ID

Usage:
    python manage.py import_simulator_bookings /path/to/simulator_bookings.csv
"""

import csv
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from datetime import datetime
from users.models import User
from bookings.models import Booking
from coaching.models import CoachingPackagePurchase, SimulatorPackagePurchase
from simulators.models import Simulator


class Command(BaseCommand):
    help = 'Import simulator session bookings from CSV file'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to the CSV file containing booking data'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate CSV without creating bookings (for testing)',
        )
        parser.add_argument(
            '--skip-errors',
            action='store_true',
            help='Continue processing even if some rows fail',
        )

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        dry_run = options['dry_run']
        skip_errors = options['skip_errors']

        # Validate file exists
        try:
            with open(csv_file_path, 'r', encoding='utf-8') as f:
                pass
        except FileNotFoundError:
            raise CommandError(f'CSV file not found: {csv_file_path}')
        except Exception as e:
            raise CommandError(f'Error reading CSV file: {e}')

        # Statistics
        stats = {
            'total': 0,
            'created': 0,
            'skipped': 0,
            'errors': 0,
            'error_details': [],
        }

        # Process CSV
        self.stdout.write(self.style.SUCCESS(f'\nStarting import from: {csv_file_path}'))
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No bookings will be created\n'))
        else:
            self.stdout.write('Creating bookings...\n')

        try:
            with open(csv_file_path, 'r', encoding='utf-8-sig') as csvfile:
                # Try to detect delimiter
                sample = csvfile.read(1024)
                csvfile.seek(0)
                
                # Try to detect delimiter, default to comma if detection fails
                delimiter = ','
                try:
                    sniffer = csv.Sniffer()
                    delimiter = sniffer.sniff(sample).delimiter
                except (csv.Error, AttributeError):
                    # If detection fails, try common delimiters
                    if ',' in sample:
                        delimiter = ','
                    elif ';' in sample:
                        delimiter = ';'
                    elif '\t' in sample:
                        delimiter = '\t'
                    # Default to comma if nothing found

                reader = csv.DictReader(csvfile, delimiter=delimiter)
                
                # Validate headers
                required_headers = ['customer_identifier', 'start_time', 'end_time', 'duration_minutes']
                missing_headers = [h for h in required_headers if h not in reader.fieldnames]
                if missing_headers:
                    raise CommandError(f'Missing required CSV headers: {", ".join(missing_headers)}')

                # Process each row
                for row_num, row in enumerate(reader, start=2):  # Start at 2 (1 is header)
                    stats['total'] += 1
                    try:
                        self._process_row(
                            row, 
                            row_num, 
                            dry_run,
                            stats
                        )
                    except Exception as e:
                        stats['errors'] += 1
                        error_msg = f'Row {row_num}: {str(e)}'
                        stats['error_details'].append(error_msg)
                        if skip_errors:
                            self.stdout.write(
                                self.style.ERROR(error_msg)
                            )
                        else:
                            raise CommandError(error_msg)

        except Exception as e:
            if not skip_errors:
                raise CommandError(f'Error processing CSV: {e}')

        # Print summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write(self.style.SUCCESS('IMPORT SUMMARY'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(f'Total rows processed: {stats["total"]}')
        self.stdout.write(self.style.SUCCESS(f'Bookings created: {stats["created"]}'))
        self.stdout.write(self.style.WARNING(f'Bookings skipped: {stats["skipped"]}'))
        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {stats["errors"]}'))
            if stats['error_details']:
                self.stdout.write('\nError Details:')
                for error in stats['error_details']:
                    self.stdout.write(self.style.ERROR(f'  - {error}'))
        self.stdout.write(self.style.SUCCESS('='*60 + '\n'))

    def _process_row(self, row, row_num, dry_run, stats):
        """Process a single CSV row"""
        # Extract and clean data
        customer_identifier = (row.get('customer_identifier') or '').strip()
        start_time_str = (row.get('start_time') or '').strip()
        end_time_str = (row.get('end_time') or '').strip()
        duration_minutes_str = (row.get('duration_minutes') or '').strip()
        simulator_bay_number = (row.get('simulator_bay_number') or '').strip()
        status = (row.get('status') or 'confirmed').strip().lower()
        location_id = (row.get('location_id') or '').strip() or None

        # Validate required fields
        if not customer_identifier:
            raise ValueError('customer_identifier is required')
        if not start_time_str:
            raise ValueError('start_time is required')
        if not end_time_str:
            raise ValueError('end_time is required')
        if not duration_minutes_str:
            raise ValueError('duration_minutes is required')

        # Parse dates
        try:
            start_time = self._parse_datetime(start_time_str)
            end_time = self._parse_datetime(end_time_str)
        except ValueError as e:
            raise ValueError(f'Invalid date format: {e}')

        # Validate date logic
        if start_time >= end_time:
            raise ValueError('end_time must be after start_time')

        # Parse duration
        try:
            duration_minutes = int(duration_minutes_str)
            if duration_minutes <= 0:
                raise ValueError('duration_minutes must be greater than 0')
        except ValueError:
            raise ValueError(f'Invalid duration_minutes: {duration_minutes_str}')

        # Calculate hours needed
        hours_needed = Decimal(str(duration_minutes)) / Decimal('60')

        # Clean phone number
        customer_phone = self._clean_phone(customer_identifier)
        if not customer_phone or len(customer_phone) < 10:
            raise ValueError(f'Invalid customer phone number: {customer_identifier}')

        # Find customer
        try:
            customer = User.objects.get(phone=customer_phone)
        except User.DoesNotExist:
            raise ValueError(f'Customer not found with phone: {customer_identifier} (cleaned: {customer_phone})')
        except User.MultipleObjectsReturned:
            raise ValueError(f'Multiple customers found with phone: {customer_identifier}')

        # Validate status
        valid_statuses = ['confirmed', 'completed', 'cancelled', 'no_show']
        if status not in valid_statuses:
            raise ValueError(f'Invalid status: {status}. Must be one of: {", ".join(valid_statuses)}')

        # Find or assign simulator
        simulator = None
        if simulator_bay_number:
            try:
                bay_num = int(simulator_bay_number)
                simulator_qs = Simulator.objects.filter(
                    bay_number=bay_num,
                    is_active=True,
                    is_coaching_bay=False
                )
                if location_id:
                    simulator_qs = simulator_qs.filter(location_id=location_id)
                
                simulator = simulator_qs.first()
                if not simulator:
                    raise ValueError(f'Simulator with bay number {bay_num} not found')
            except ValueError:
                raise ValueError(f'Invalid simulator_bay_number: {simulator_bay_number}')

        # Find available package purchase (combo or simulator-only)
        package_purchase = self._find_available_package_purchase(customer, hours_needed, location_id)
        
        # Determine package purchase types
        combo_package_purchase = None
        simulator_package_purchase = None
        if package_purchase:
            if isinstance(package_purchase, SimulatorPackagePurchase):
                simulator_package_purchase = package_purchase
            else:
                combo_package_purchase = package_purchase

        # If no package found, we can still create the booking (paid booking)
        # But we'll skip if no package and no simulator specified
        if not package_purchase and not simulator:
            stats['skipped'] += 1
            self.stdout.write(
                self.style.WARNING(
                    f'Row {row_num}: Skipping - No available package purchase found and no simulator specified '
                    f'for customer {customer.phone}'
                )
            )
            return

        # If no simulator specified, try to find one
        if not simulator:
            simulator = self._find_available_simulator(start_time, end_time, location_id)
            if not simulator:
                stats['skipped'] += 1
                self.stdout.write(
                    self.style.WARNING(
                        f'Row {row_num}: Skipping - No available simulator found for time slot'
                    )
                )
                return

        # Calculate price (0 if using package, otherwise use simulator hourly rate)
        total_price = Decimal('0.00')
        if not package_purchase and simulator.hourly_price:
            total_price = (Decimal(str(simulator.hourly_price)) * hours_needed).quantize(Decimal('0.01'))

        # Create booking
        if not dry_run:
            with transaction.atomic():
                # Consume hours from package if using package
                if package_purchase:
                    if isinstance(package_purchase, SimulatorPackagePurchase):
                        package_purchase.consume_hours(hours_needed)
                    else:
                        # Combo package
                        package_purchase.consume_simulator_hours(hours_needed)
                
                # Create booking
                booking = Booking.objects.create(
                    client=customer,
                    location_id=location_id,
                    booking_type='simulator',
                    status=status,
                    simulator=simulator,
                    duration_minutes=duration_minutes,
                    start_time=start_time,
                    end_time=end_time,
                    total_price=total_price,
                    package_purchase=combo_package_purchase,
                    simulator_package_purchase=simulator_package_purchase
                )
                
                stats['created'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Row {row_num}: Created booking #{booking.id} for {customer.phone} '
                        f'(Simulator {simulator.bay_number}) on {start_time.strftime("%Y-%m-%d %H:%M")}'
                    )
                )
        else:
            stats['created'] += 1
            package_info = 'with package' if package_purchase else 'paid booking'
            self.stdout.write(
                self.style.SUCCESS(
                    f'Row {row_num}: [DRY RUN] Would create booking for {customer.phone} '
                    f'(Simulator {simulator.bay_number if simulator else "TBD"}) '
                    f'on {start_time.strftime("%Y-%m-%d %H:%M")} {package_info}'
                )
            )

    def _find_available_package_purchase(self, customer, hours_needed, location_id=None):
        """
        Find an available package purchase (combo or simulator-only) with enough hours.
        Uses FIFO (first-in-first-out) - oldest purchase first.
        """
        # First try combo packages (CoachingPackagePurchase with simulator hours)
        combo_purchase = CoachingPackagePurchase.objects.filter(
            client=customer,
            simulator_hours_remaining__gte=hours_needed,
            package_status='active'
        ).exclude(
            gift_status='pending'
        ).exclude(
            purchase_type='organization'
        ).order_by('purchased_at').first()
        
        if combo_purchase:
            return combo_purchase
        
        # If no combo package, try simulator-only packages
        sim_purchase = SimulatorPackagePurchase.objects.filter(
            client=customer,
            hours_remaining__gte=hours_needed,
            package_status='active'
        ).exclude(
            gift_status='pending'
        ).order_by('purchased_at').first()
        
        return sim_purchase

    def _find_available_simulator(self, start_time, end_time, location_id=None):
        """
        Find an available simulator for the given time slot.
        Returns the first available simulator.
        """
        simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        )
        
        if location_id:
            simulators = simulators.filter(location_id=location_id)
        
        simulators = simulators.order_by('bay_number')
        
        for simulator in simulators:
            # Check for conflicts
            conflict_exists = Booking.objects.filter(
                simulator=simulator,
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed'],
                booking_type='simulator'
            ).exists()
            
            if not conflict_exists:
                return simulator
        
        return None

    def _parse_datetime(self, date_str):
        """
        Parse datetime string. Tries DD-MM-YYYY first (user's format), then other formats.
        """
        date_str = date_str.strip()
        
        # Try DD-MM-YYYY HH:MM format first (user's actual format)
        try:
            return datetime.strptime(date_str, '%d-%m-%Y %H:%M')
        except ValueError:
            pass
        
        # Try DD/MM/YYYY HH:MM format
        try:
            return datetime.strptime(date_str, '%d/%m/%Y %H:%M')
        except ValueError:
            pass
        
        # Try MM-DD-YYYY HH:MM format (US format)
        try:
            return datetime.strptime(date_str, '%m-%d-%Y %H:%M')
        except ValueError:
            pass
        
        # Try MM/DD/YYYY HH:MM format
        try:
            return datetime.strptime(date_str, '%m/%d/%Y %H:%M')
        except ValueError:
            pass
        
        # Try YYYY-MM-DD HH:MM:SS format
        try:
            return datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass
        
        # Try YYYY-MM-DD HH:MM format
        try:
            return datetime.strptime(date_str, '%Y-%m-%d %H:%M')
        except ValueError:
            pass
        
        # Try ISO format with timezone
        try:
            from django.utils.dateparse import parse_datetime
            parsed = parse_datetime(date_str)
            if parsed:
                return parsed
        except:
            pass
        
        raise ValueError(f'Unable to parse date: {date_str}. Expected format: DD-MM-YYYY HH:MM')

    def _clean_phone(self, phone):
        """Clean phone number - remove common formatting"""
        if not phone:
            return ''
        # Remove common formatting characters
        cleaned = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        return cleaned

