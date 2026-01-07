"""
Django management command to import coaching session bookings from CSV file.

This command reads a CSV file with booking data and creates:
1. Booking records for coaching sessions
2. Links bookings to package purchases
3. Consumes sessions from packages (reduces sessions_remaining)

CSV Format Expected:
- customer_identifier: Phone number (e.g., (902) 483-5479 or 9024835479)
- start_time: DateTime in MM-DD-YYYY HH:MM format (e.g., 07-01-2026 10:00)
- end_time: DateTime in MM-DD-YYYY HH:MM format (e.g., 07-01-2026 11:00)
- coaching_package_name: Exact package title (must match existing package)
- coach_identifier: Phone number of the coach (e.g., 9028189870)
- status: Booking status (confirmed, completed, cancelled, no_show) - defaults to 'confirmed'
- location_id: Optional GHL location ID

Usage:
    python manage.py import_coaching_bookings /path/to/coaching_bookings.csv
"""

import csv
from decimal import Decimal, ROUND_HALF_UP
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from datetime import datetime
from users.models import User
from bookings.models import Booking
from coaching.models import CoachingPackage, CoachingPackagePurchase


class Command(BaseCommand):
    help = 'Import coaching session bookings from CSV file'

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
            'skipped_duplicates': 0,
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
                required_headers = ['customer_identifier', 'start_time', 'end_time', 'coaching_package_name', 'coach_identifier']
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
        self.stdout.write(self.style.WARNING(f'Bookings skipped (no package): {stats["skipped"]}'))
        self.stdout.write(self.style.WARNING(f'Bookings skipped (duplicates): {stats["skipped_duplicates"]}'))
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
        coaching_package_name = (row.get('coaching_package_name') or '').strip()
        coach_identifier = (row.get('coach_identifier') or '').strip()
        status = (row.get('status') or 'confirmed').strip().lower()
        location_id = (row.get('location_id') or '').strip() or None

        # Validate required fields
        if not customer_identifier:
            raise ValueError('customer_identifier is required')
        if not start_time_str:
            raise ValueError('start_time is required')
        if not end_time_str:
            raise ValueError('end_time is required')
        if not coaching_package_name:
            raise ValueError('coaching_package_name is required')
        if not coach_identifier:
            raise ValueError('coach_identifier is required')

        # Parse dates (MM-DD-YYYY HH:MM format)
        try:
            start_time = self._parse_datetime(start_time_str)
            end_time = self._parse_datetime(end_time_str)
        except ValueError as e:
            raise ValueError(f'Invalid date format: {e}')

        # Validate date logic
        if start_time >= end_time:
            raise ValueError('end_time must be after start_time')

        # Clean phone numbers
        customer_phone = self._clean_phone(customer_identifier)
        coach_phone = self._clean_phone(coach_identifier)

        if not customer_phone or len(customer_phone) < 10:
            raise ValueError(f'Invalid customer phone number: {customer_identifier}')
        if not coach_phone or len(coach_phone) < 10:
            raise ValueError(f'Invalid coach phone number: {coach_identifier}')

        # Find customer
        try:
            customer = User.objects.get(phone=customer_phone)
        except User.DoesNotExist:
            raise ValueError(f'Customer not found with phone: {customer_identifier} (cleaned: {customer_phone})')
        except User.MultipleObjectsReturned:
            raise ValueError(f'Multiple customers found with phone: {customer_identifier}')

        # Find coach
        try:
            coach = User.objects.get(phone=coach_phone, role__in=['staff', 'admin'])
        except User.DoesNotExist:
            raise ValueError(f'Coach not found with phone: {coach_identifier} (cleaned: {coach_phone})')
        except User.MultipleObjectsReturned:
            raise ValueError(f'Multiple coaches found with phone: {coach_identifier}')

        # Find coaching package
        package = None
        # Try exact match first
        packages = CoachingPackage.objects.filter(title=coaching_package_name, is_active=True)
        
        # If location_id provided, filter by location
        if location_id:
            packages = packages.filter(location_id=location_id)
        
        if packages.count() == 0:
            # Try case-insensitive match
            packages = CoachingPackage.objects.filter(
                title__iexact=coaching_package_name,
                is_active=True
            )
            if location_id:
                packages = packages.filter(location_id=location_id)
        
        if packages.count() == 0:
            raise ValueError(f'Coaching package not found: {coaching_package_name}' + 
                           (f' for location: {location_id}' if location_id else ''))
        elif packages.count() == 1:
            package = packages.first()
        else:
            # Multiple packages found - use the first one, but warn
            package = packages.first()
            self.stdout.write(
                self.style.WARNING(
                    f'Row {row_num}: Multiple packages found with name "{coaching_package_name}". '
                    f'Using package ID {package.id}. Consider specifying location_id in CSV.'
                )
            )

        # Validate status
        valid_statuses = ['confirmed', 'completed', 'cancelled', 'no_show']
        if status not in valid_statuses:
            raise ValueError(f'Invalid status: {status}. Must be one of: {", ".join(valid_statuses)}')

        # Find available package purchase
        purchase = self._find_available_package_purchase(customer, package)
        if not purchase:
            stats['skipped'] += 1
            self.stdout.write(
                self.style.WARNING(
                    f'Row {row_num}: Skipping - No available package purchase found for '
                    f'customer {customer.phone} and package "{coaching_package_name}"'
                )
            )
            return

        # Check for time conflicts (optional - can be skipped for historical imports)
        # We'll skip this for now since these are historical bookings

        # Check if booking already exists
        existing_booking = Booking.objects.filter(
            client=customer,
            booking_type='coaching',
            start_time=start_time,
            end_time=end_time,
            coach=coach,
            coaching_package=package
        ).first()
        
        if existing_booking:
            stats['skipped_duplicates'] += 1
            self.stdout.write(
                self.style.WARNING(
                    f'Row {row_num}: Skipping - Booking already exists (ID: {existing_booking.id}) '
                    f'for {customer.phone} on {start_time.strftime("%Y-%m-%d %H:%M")}'
                )
            )
            return

        # Calculate price per session
        if package.session_count and package.session_count > 0:
            per_session = (Decimal(str(package.price)) / Decimal(str(package.session_count))).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        else:
            per_session = Decimal(str(package.price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Create booking
        if not dry_run:
            with transaction.atomic():
                # Consume session from package
                purchase.consume_session(1)
                
                # Check if package is TPI assessment
                is_tpi_assessment = getattr(package, 'is_tpi_assessment', False)
                
                # Create booking
                booking = Booking.objects.create(
                    client=customer,
                    location_id=location_id,
                    booking_type='coaching',
                    status=status,
                    coaching_package=package,
                    coach=coach,
                    package_purchase=purchase,
                    start_time=start_time,
                    end_time=end_time,
                    duration_minutes=package.session_duration_minutes,
                    total_price=per_session,
                    is_tpi_assessment=is_tpi_assessment
                )
                
                stats['created'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Row {row_num}: Created booking #{booking.id} for {customer.phone} '
                        f'({coaching_package_name}) on {start_time.strftime("%Y-%m-%d %H:%M")}'
                    )
                )
        else:
            stats['created'] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f'Row {row_num}: [DRY RUN] Would create booking for {customer.phone} '
                    f'({coaching_package_name}) on {start_time.strftime("%Y-%m-%d %H:%M")}'
                )
            )

    def _find_available_package_purchase(self, customer, package):
        """
        Find an available package purchase for the customer and package.
        Uses FIFO (first-in-first-out) - oldest purchase first.
        """
        purchase = CoachingPackagePurchase.objects.filter(
            client=customer,
            package=package,
            sessions_remaining__gt=0,
            package_status='active'
        ).exclude(
            gift_status='pending'
        ).exclude(
            purchase_type='organization'
        ).order_by('purchased_at').first()
        
        return purchase

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

