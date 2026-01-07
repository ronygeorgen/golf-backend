"""
Django management command to fix booking dates that were incorrectly parsed.

This script reads the CSV file again and updates bookings that were created
with incorrect dates (parsed as MM-DD-YYYY instead of DD-MM-YYYY).

Usage:
    python manage.py fix_booking_dates "path/to/coaching_bookings.csv" --dry-run
    python manage.py fix_booking_dates "path/to/coaching_bookings.csv"
"""

import csv
from decimal import Decimal, ROUND_HALF_UP
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from datetime import datetime
from bookings.models import Booking
from users.models import User


class Command(BaseCommand):
    help = 'Fix booking dates that were incorrectly parsed from CSV'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to the CSV file containing booking data'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be fixed without making changes',
        )

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        dry_run = options['dry_run']

        # Validate file exists
        try:
            with open(csv_file_path, 'r', encoding='utf-8-sig') as f:
                pass
        except FileNotFoundError:
            raise CommandError(f'CSV file not found: {csv_file_path}')
        except Exception as e:
            raise CommandError(f'Error reading CSV file: {e}')

        stats = {
            'total': 0,
            'fixed': 0,
            'not_found': 0,
            'already_correct': 0,
            'errors': 0,
            'error_details': []
        }

        self.stdout.write(self.style.SUCCESS(f'\nStarting date fix from: {csv_file_path}'))
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made\n'))
        else:
            self.stdout.write('Fixing booking dates...\n')

        try:
            with open(csv_file_path, 'r', encoding='utf-8-sig') as csvfile:
                # Try to detect delimiter
                sample = csvfile.read(1024)
                csvfile.seek(0)
                
                delimiter = ','
                try:
                    import csv as csv_module
                    sniffer = csv_module.Sniffer()
                    delimiter = sniffer.sniff(sample).delimiter
                except (csv_module.Error, AttributeError):
                    if ',' in sample:
                        delimiter = ','
                    elif ';' in sample:
                        delimiter = ';'
                    elif '\t' in sample:
                        delimiter = '\t'

                reader = csv.DictReader(csvfile, delimiter=delimiter)
                
                # Validate headers
                required_headers = ['customer_identifier', 'start_time', 'end_time']
                missing_headers = [h for h in required_headers if h not in reader.fieldnames]
                if missing_headers:
                    raise CommandError(f'Missing required CSV headers: {", ".join(missing_headers)}')

                # Process each row
                for row_num, row in enumerate(reader, start=2):
                    stats['total'] += 1
                    try:
                        self._fix_booking_date(
                            row, 
                            row_num, 
                            dry_run,
                            stats
                        )
                    except Exception as e:
                        stats['errors'] += 1
                        error_msg = f'Row {row_num}: {str(e)}'
                        stats['error_details'].append(error_msg)
                        self.stdout.write(self.style.ERROR(error_msg))

        except Exception as e:
            raise CommandError(f'Error processing CSV: {e}')

        # Print summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write('DATE FIX SUMMARY')
        self.stdout.write('='*60)
        self.stdout.write(f'Total rows processed: {stats["total"]}')
        self.stdout.write(self.style.SUCCESS(f'Bookings fixed: {stats["fixed"]}'))
        self.stdout.write(self.style.WARNING(f'Already correct: {stats["already_correct"]}'))
        self.stdout.write(self.style.WARNING(f'Not found: {stats["not_found"]}'))
        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {stats["errors"]}'))
            if stats['error_details']:
                self.stdout.write('\nError Details:')
                for error in stats['error_details']:
                    self.stdout.write(self.style.ERROR(f'  - {error}'))
        self.stdout.write('='*60 + '\n')

    def _fix_booking_date(self, row, row_num, dry_run, stats):
        """Fix the date for a single booking"""
        customer_identifier = (row.get('customer_identifier') or '').strip()
        start_time_str = (row.get('start_time') or '').strip()
        end_time_str = (row.get('end_time') or '').strip()

        if not customer_identifier or not start_time_str or not end_time_str:
            return  # Skip rows with missing data

        # Parse dates as DD-MM-YYYY
        try:
            correct_start_time = datetime.strptime(start_time_str, '%d-%m-%Y %H:%M')
            correct_end_time = datetime.strptime(end_time_str, '%d-%m-%Y %H:%M')
        except ValueError:
            # Try other formats
            try:
                correct_start_time = datetime.strptime(start_time_str, '%d/%m/%Y %H:%M')
                correct_end_time = datetime.strptime(end_time_str, '%d/%m/%Y %H:%M')
            except ValueError:
                raise ValueError(f'Unable to parse dates: {start_time_str}, {end_time_str}')

        # Clean phone number
        customer_phone = self._clean_phone(customer_identifier)
        if not customer_phone or len(customer_phone) < 10:
            return

        # Find customer
        try:
            customer = User.objects.get(phone=customer_phone)
        except User.DoesNotExist:
            stats['not_found'] += 1
            return
        except User.MultipleObjectsReturned:
            raise ValueError(f'Multiple customers found with phone: {customer_identifier}')

        # Find booking by customer and approximate time (within 30 days to handle month swap)
        # We'll look for bookings where the date might be wrong
        bookings = Booking.objects.filter(
            client=customer,
            booking_type='coaching'
        ).order_by('-created_at')
        
        # Try to find booking by matching the day and time (ignoring month/year)
        matching_booking = None
        for booking in bookings:
            # Check if day and time match (month might be swapped)
            if (booking.start_time.day == correct_start_time.day and
                booking.start_time.hour == correct_start_time.hour and
                booking.start_time.minute == correct_start_time.minute):
                matching_booking = booking
                break
        
        if not matching_booking:
            stats['not_found'] += 1
            self.stdout.write(
                self.style.WARNING(
                    f'Row {row_num}: No matching booking found for {customer.phone} '
                    f'at {correct_start_time.strftime("%d-%m-%Y %H:%M")}'
                )
            )
            return

        # Check if dates are already correct
        if (matching_booking.start_time == correct_start_time and 
            matching_booking.end_time == correct_end_time):
            stats['already_correct'] += 1
            return

        # Calculate new end_time based on duration if needed
        duration = matching_booking.end_time - matching_booking.start_time
        new_end_time = correct_start_time + duration

        if dry_run:
            self.stdout.write(
                f'Row {row_num}: [DRY RUN] Would fix booking ID {matching_booking.id} for {customer.phone}:'
            )
            self.stdout.write(
                f'   Start: {matching_booking.start_time} → {correct_start_time}'
            )
            self.stdout.write(
                f'   End: {matching_booking.end_time} → {new_end_time}'
            )
            stats['fixed'] += 1
            return

        # Update booking
        with transaction.atomic():
            matching_booking.start_time = correct_start_time
            matching_booking.end_time = new_end_time
            matching_booking.save(update_fields=['start_time', 'end_time', 'updated_at'])
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'Row {row_num}: Fixed booking ID {matching_booking.id} for {customer.phone}: '
                    f'{matching_booking.start_time.strftime("%Y-%m-%d %H:%M")} → '
                    f'{correct_start_time.strftime("%Y-%m-%d %H:%M")}'
                )
            )
            stats['fixed'] += 1

    def _clean_phone(self, phone):
        """Clean phone number - remove common formatting"""
        if not phone:
            return ''
        cleaned = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        return cleaned

