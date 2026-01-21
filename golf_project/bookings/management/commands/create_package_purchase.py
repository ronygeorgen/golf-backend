"""
Django management command to create a package purchase for a customer.

Usage:
    python manage.py create_package_purchase <phone> --package-name "Package Name" --sessions 10 --sim-hours 5.0
    python manage.py create_package_purchase <phone> --package-id 1 --sessions 10
"""

from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from users.models import User
from coaching.models import CoachingPackage, CoachingPackagePurchase


class Command(BaseCommand):
    help = 'Create a package purchase for a customer'

    def add_arguments(self, parser):
        parser.add_argument(
            'phone',
            type=str,
            help='Customer phone number'
        )
        parser.add_argument(
            '--package-name',
            type=str,
            help='Package name (exact match)',
        )
        parser.add_argument(
            '--package-id',
            type=int,
            help='Package ID',
        )
        parser.add_argument(
            '--sessions',
            type=int,
            default=0,
            help='Number of sessions (default: uses package session_count)',
        )
        parser.add_argument(
            '--sim-hours',
            type=float,
            default=0.0,
            help='Number of simulator hours (default: uses package simulator_hours)',
        )
        parser.add_argument(
            '--purchase-name',
            type=str,
            help='Custom purchase name (default: package title)',
        )
        parser.add_argument(
            '--notes',
            type=str,
            default='',
            help='Notes for this purchase',
        )
        parser.add_argument(
            '--location-id',
            type=str,
            help='Location ID to filter packages',
        )

    def handle(self, *args, **options):
        phone = options['phone']
        package_name = options.get('package_name')
        package_id = options.get('package_id')
        sessions = options.get('sessions')
        sim_hours = Decimal(str(options.get('sim_hours', 0.0)))
        purchase_name = options.get('purchase_name')
        notes = options.get('notes', '')
        location_id = options.get('location_id')
        
        # Clean phone number
        cleaned_phone = self._clean_phone(phone)
        
        # Find customer
        try:
            customer = User.objects.get(phone=cleaned_phone)
        except User.DoesNotExist:
            raise CommandError(f'Customer not found with phone: {phone} (cleaned: {cleaned_phone})')
        except User.MultipleObjectsReturned:
            raise CommandError(f'Multiple customers found with phone: {phone}')
        
        self.stdout.write(f'Customer: {customer.first_name} {customer.last_name} ({customer.phone})')
        
        # Find package
        package = None
        if package_id:
            try:
                package = CoachingPackage.objects.get(id=package_id, is_active=True)
            except CoachingPackage.DoesNotExist:
                raise CommandError(f'Package with ID {package_id} not found or not active')
        elif package_name:
            packages = CoachingPackage.objects.filter(title=package_name, is_active=True)
            if location_id:
                packages = packages.filter(location_id=location_id)
            
            if packages.count() == 0:
                # Try case-insensitive
                packages = CoachingPackage.objects.filter(title__iexact=package_name, is_active=True)
                if location_id:
                    packages = packages.filter(location_id=location_id)
            
            if packages.count() == 0:
                raise CommandError(f'Package "{package_name}" not found' + 
                                (f' for location: {location_id}' if location_id else ''))
            elif packages.count() > 1:
                self.stdout.write(self.style.WARNING(f'Multiple packages found with name "{package_name}":'))
                for pkg in packages:
                    self.stdout.write(f'  - ID: {pkg.id}, Title: {pkg.title}, Location: {pkg.location_id or "All"}')
                raise CommandError('Please specify --package-id to select a specific package')
            else:
                package = packages.first()
        else:
            raise CommandError('Must specify either --package-name or --package-id')
        
        self.stdout.write(f'Package: {package.title} (ID: {package.id})')
        
        # Determine sessions and sim hours
        if sessions == 0:
            sessions = package.session_count or 0
        
        if sim_hours == 0:
            sim_hours = Decimal(str(package.simulator_hours)) if package.simulator_hours else Decimal('0')
        
        # Use package title as purchase name if not specified
        if not purchase_name:
            purchase_name = package.title
        
        # Create purchase
        purchase = CoachingPackagePurchase.objects.create(
            client=customer,
            package=package,
            purchase_name=purchase_name,
            sessions_total=sessions,
            sessions_remaining=sessions,
            simulator_hours_total=sim_hours,
            simulator_hours_remaining=sim_hours,
            notes=notes,
            purchase_type='normal',
            package_status='active',
        )
        
        self.stdout.write(self.style.SUCCESS(f'\n✅ Package purchase created successfully!'))
        self.stdout.write(f'Purchase ID: {purchase.id}')
        self.stdout.write(f'Sessions: {purchase.sessions_remaining}/{purchase.sessions_total}')
        self.stdout.write(f'Simulator Hours: {purchase.simulator_hours_remaining}/{purchase.simulator_hours_total}')
    
    def _clean_phone(self, phone):
        """Clean phone number - remove common formatting"""
        if not phone:
            return ''
        cleaned = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        return cleaned




