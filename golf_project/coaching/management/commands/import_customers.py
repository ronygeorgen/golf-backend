"""
Django management command to import existing customers from CSV file.

This command reads a CSV file with customer data and creates:
1. User accounts (if they don't exist)
2. CoachingPackagePurchase records with remaining lessons and sim hours

CSV Format Expected:
- First Name
- Last Name
- Email
- Phone
- Remaining Lessons
- Remaining Sim Hours
- Package Name
- Notes (optional)

Usage:
    python manage.py import_customers /path/to/customers.csv
"""

import csv
import secrets
from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from users.models import User
from coaching.models import CoachingPackage, CoachingPackagePurchase


class Command(BaseCommand):
    help = 'Import existing customers from CSV file'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to the CSV file containing customer data'
        )
        parser.add_argument(
            '--skip-existing',
            action='store_true',
            help='Skip customers that already exist (by phone or email)',
        )
        parser.add_argument(
            '--update-existing',
            action='store_true',
            help='Update existing customers and add package purchases',
        )
        parser.add_argument(
            '--default-package',
            type=int,
            help='ID of existing CoachingPackage to use for all imports (if not provided, will create a default package)',
        )

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        skip_existing = options['skip_existing']
        update_existing = options['update_existing']
        default_package_id = options.get('default_package')

        # Validate file exists
        try:
            with open(csv_file_path, 'r', encoding='utf-8') as f:
                pass
        except FileNotFoundError:
            raise CommandError(f'CSV file not found: {csv_file_path}')
        except Exception as e:
            raise CommandError(f'Error reading CSV file: {e}')

        # Get or create default package
        package = self._get_or_create_package(default_package_id)

        # Statistics
        stats = {
            'total': 0,
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'errors': 0,
            'purchases_created': 0,
        }

        # Process CSV
        self.stdout.write(self.style.SUCCESS(f'\nStarting import from: {csv_file_path}'))
        self.stdout.write(f'Using package: {package.title} (ID: {package.id})\n')

        try:
            with open(csv_file_path, 'r', encoding='utf-8') as csvfile:
                # Try to detect delimiter
                sample = csvfile.read(1024)
                csvfile.seek(0)
                sniffer = csv.Sniffer()
                delimiter = sniffer.sniff(sample).delimiter

                reader = csv.DictReader(csvfile, delimiter=delimiter)
                
                # Validate headers
                required_headers = ['First Name', 'Last Name', 'Email', 'Phone', 'Remaining Lessons', 'Remaining Sim Hours', 'Package Name']
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
                            package, 
                            skip_existing, 
                            update_existing, 
                            stats
                        )
                    except Exception as e:
                        stats['errors'] += 1
                        self.stdout.write(
                            self.style.ERROR(f'Row {row_num}: Error - {str(e)}')
                        )

        except Exception as e:
            raise CommandError(f'Error processing CSV: {e}')

        # Print summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write(self.style.SUCCESS('IMPORT SUMMARY'))
        self.stdout.write(self.style.SUCCESS('='*60))
        self.stdout.write(f'Total rows processed: {stats["total"]}')
        self.stdout.write(self.style.SUCCESS(f'Users created: {stats["created"]}'))
        self.stdout.write(self.style.SUCCESS(f'Users updated: {stats["updated"]}'))
        self.stdout.write(self.style.WARNING(f'Users skipped: {stats["skipped"]}'))
        self.stdout.write(self.style.SUCCESS(f'Package purchases created: {stats["purchases_created"]}'))
        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {stats["errors"]}'))
        self.stdout.write(self.style.SUCCESS('='*60 + '\n'))

    def _get_or_create_package(self, package_id=None):
        """Get existing package or create a default one for imports"""
        if package_id:
            try:
                package = CoachingPackage.objects.get(id=package_id)
                self.stdout.write(f'Using existing package: {package.title}')
                return package
            except CoachingPackage.DoesNotExist:
                raise CommandError(f'Package with ID {package_id} does not exist')

        # Create a default package for legacy imports
        package, created = CoachingPackage.objects.get_or_create(
            title='Legacy Import Package',
            defaults={
                'description': 'Default package created for importing legacy customer data',
                'price': Decimal('0.00'),
                'session_count': 1,
                'session_duration_minutes': 60,
                'simulator_hours': Decimal('0.00'),
                'is_active': True,
            }
        )

        if created:
            self.stdout.write(
                self.style.WARNING(
                    f'Created default package: {package.title} (ID: {package.id})'
                )
            )
        else:
            self.stdout.write(f'Using existing default package: {package.title}')

        return package

    def _process_row(self, row, row_num, package, skip_existing, update_existing, stats):
        """Process a single CSV row"""
        # Extract and clean data
        first_name = (row.get('First Name') or '').strip()
        last_name = (row.get('Last Name') or '').strip()
        email = (row.get('Email') or '').strip().lower()
        phone = self._clean_phone(row.get('Phone', '').strip())
        remaining_lessons = self._parse_int(row.get('Remaining Lessons', '0').strip())
        remaining_sim_hours = self._parse_decimal(row.get('Remaining Sim Hours', '0').strip())
        package_name = (row.get('Package Name') or '').strip()
        notes = (row.get('Notes') or '').strip()

        # Validate required fields
        if not first_name:
            raise ValueError('First Name is required')
        if not last_name:
            raise ValueError('Last Name is required')
        if not email:
            raise ValueError('Email is required')
        if not phone:
            raise ValueError('Phone is required')
        if len(phone) < 10 or len(phone) > 15:
            raise ValueError(f'Invalid phone number format: {phone} (must be 10-15 digits)')

        # Check if user exists
        user = None
        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            try:
                if email:
                    user = User.objects.get(email=email)
            except User.DoesNotExist:
                pass

        # Handle existing user
        if user:
            if skip_existing:
                stats['skipped'] += 1
                self.stdout.write(
                    self.style.WARNING(f'Row {row_num}: Skipping existing user {user.email} ({user.phone})')
                )
                return

            if update_existing:
                # Update user info
                user.first_name = first_name
                user.last_name = last_name
                if email and not User.objects.filter(email=email).exclude(id=user.id).exists():
                    user.email = email
                user.save(update_fields=['first_name', 'last_name', 'email'])
                stats['updated'] += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Row {row_num}: Updated user {user.email} ({user.phone})')
                )
            else:
                # Just use existing user without updating
                self.stdout.write(
                    self.style.WARNING(f'Row {row_num}: Using existing user {user.email} ({user.phone})')
                )
        else:
            # Create new user
            user = self._create_user(first_name, last_name, email, phone)
            stats['created'] += 1
            self.stdout.write(
                self.style.SUCCESS(f'Row {row_num}: Created user {user.email} ({user.phone})')
            )

        # Create package purchase if there are remaining lessons or sim hours
        if remaining_lessons > 0 or remaining_sim_hours > 0:
            purchase = self._create_package_purchase(
                user, 
                package, 
                package_name, 
                remaining_lessons, 
                remaining_sim_hours, 
                notes
            )
            stats['purchases_created'] += 1
            self.stdout.write(
                f'  → Created package purchase: {purchase.purchase_name} '
                f'({purchase.sessions_remaining} lessons, {purchase.simulator_hours_remaining} sim hours)'
            )

    def _create_user(self, first_name, last_name, email, phone):
        """Create a new user with auto-generated username"""
        # Generate username from email
        username = email.split('@')[0] if email else phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        
        # Ensure username is unique
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1

        # Create user
        user = User.objects.create(
            username=username,
            email=email,
            phone=phone,
            first_name=first_name,
            last_name=last_name,
            role='client',
            email_verified=False,
            phone_verified=False,
        )

        # Set a random password (users will use OTP login)
        user.set_password(secrets.token_urlsafe(32))
        user.save()

        return user

    def _create_package_purchase(self, user, package, package_name, remaining_lessons, remaining_sim_hours, notes):
        """Create a CoachingPackagePurchase record"""
        # Use package_name from CSV, or fallback to package title
        purchase_name = package_name if package_name else package.title

        # Set total values (use remaining as total for legacy imports)
        sessions_total = max(remaining_lessons, 1)  # At least 1
        simulator_hours_total = max(remaining_sim_hours, Decimal('0'))

        purchase = CoachingPackagePurchase.objects.create(
            client=user,
            package=package,
            purchase_name=purchase_name,
            sessions_total=sessions_total,
            sessions_remaining=remaining_lessons,
            simulator_hours_total=simulator_hours_total,
            simulator_hours_remaining=remaining_sim_hours,
            notes=notes,
            purchase_type='normal',
            package_status='active',
        )

        return purchase

    def _clean_phone(self, phone):
        """Clean phone number - remove common formatting"""
        if not phone:
            return ''
        # Remove common formatting characters
        cleaned = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        return cleaned

    def _parse_int(self, value):
        """Parse integer value, return 0 if invalid"""
        try:
            return int(float(value)) if value else 0
        except (ValueError, TypeError):
            return 0

    def _parse_decimal(self, value):
        """Parse decimal value, return 0 if invalid"""
        try:
            return Decimal(str(value)) if value else Decimal('0')
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0')

