"""
Django management command to check package availability for a customer.

Usage:
    python manage.py check_package_availability <phone_number>
    python manage.py check_package_availability <phone_number> --package-name "Package Name"
"""

from django.core.management.base import BaseCommand, CommandError
from users.models import User
from coaching.models import CoachingPackage, CoachingPackagePurchase, SimulatorPackagePurchase


class Command(BaseCommand):
    help = 'Check package availability for a customer'

    def add_arguments(self, parser):
        parser.add_argument(
            'phone',
            type=str,
            help='Customer phone number'
        )
        parser.add_argument(
            '--package-name',
            type=str,
            help='Specific package name to check (optional)',
        )

    def handle(self, *args, **options):
        phone = options['phone']
        package_name = options.get('package_name')
        
        # Clean phone number
        cleaned_phone = self._clean_phone(phone)
        
        # Find customer
        try:
            customer = User.objects.get(phone=cleaned_phone)
        except User.DoesNotExist:
            raise CommandError(f'Customer not found with phone: {phone} (cleaned: {cleaned_phone})')
        except User.MultipleObjectsReturned:
            raise CommandError(f'Multiple customers found with phone: {phone}')
        
        self.stdout.write(self.style.SUCCESS(f'\nCustomer: {customer.first_name} {customer.last_name} ({customer.phone})'))
        self.stdout.write('='*60)
        
        # Check coaching packages
        if package_name:
            self._check_specific_package(customer, package_name)
        else:
            self._check_all_packages(customer)
    
    def _check_specific_package(self, customer, package_name):
        """Check availability for a specific package"""
        # Find all packages with this name
        packages = CoachingPackage.objects.filter(
            title__iexact=package_name,
            is_active=True
        )
        
        if not packages.exists():
            self.stdout.write(self.style.ERROR(f'\nNo active package found with name: "{package_name}"'))
            return
        
        if packages.count() > 1:
            self.stdout.write(self.style.WARNING(f'\n⚠️  WARNING: Multiple packages found with name "{package_name}"'))
            self.stdout.write('Packages:')
            for pkg in packages:
                self.stdout.write(f'  - ID: {pkg.id}, Title: {pkg.title}, Location: {pkg.location_id or "All"}')
        
        for package in packages:
            self.stdout.write(f'\n📦 Package: {package.title} (ID: {package.id})')
            self.stdout.write(f'   Location: {package.location_id or "All locations"}')
            
            # Check purchases for this package
            purchases = CoachingPackagePurchase.objects.filter(
                client=customer,
                package=package,
                sessions_remaining__gt=0,
                package_status='active'
            ).exclude(
                gift_status='pending'
            ).exclude(
                purchase_type='organization'
            ).order_by('purchased_at')
            
            if purchases.exists():
                self.stdout.write(self.style.SUCCESS(f'   ✅ Available purchases: {purchases.count()}'))
                for purchase in purchases:
                    self.stdout.write(
                        f'      - Purchase ID: {purchase.id}, '
                        f'Sessions: {purchase.sessions_remaining}/{purchase.sessions_total}, '
                        f'Sim Hours: {purchase.simulator_hours_remaining}/{purchase.simulator_hours_total}, '
                        f'Purchased: {purchase.purchased_at.strftime("%Y-%m-%d")}'
                    )
            else:
                self.stdout.write(self.style.ERROR(f'   ❌ No available purchases found'))
                
                # Check if they have any purchases (even if depleted)
                all_purchases = CoachingPackagePurchase.objects.filter(
                    client=customer,
                    package=package
                )
                if all_purchases.exists():
                    self.stdout.write(f'   (Total purchases: {all_purchases.count()}, but all are depleted)')
    
    def _check_all_packages(self, customer):
        """Check all packages for the customer"""
        # Get all active coaching packages
        all_packages = CoachingPackage.objects.filter(is_active=True).order_by('title')
        
        self.stdout.write(f'\n📋 All Active Packages ({all_packages.count()} total):\n')
        
        packages_with_availability = 0
        
        for package in all_packages:
            purchases = CoachingPackagePurchase.objects.filter(
                client=customer,
                package=package,
                sessions_remaining__gt=0,
                package_status='active'
            ).exclude(
                gift_status='pending'
            ).exclude(
                purchase_type='organization'
            )
            
            if purchases.exists():
                packages_with_availability += 1
                total_sessions = sum(p.sessions_remaining for p in purchases)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'✅ {package.title} - {total_sessions} sessions available '
                        f'({purchases.count()} purchase(s))'
                    )
                )
        
        if packages_with_availability == 0:
            self.stdout.write(self.style.ERROR('❌ No packages with available sessions found'))
        
        # Check simulator packages
        self.stdout.write(f'\n🎮 Simulator Packages:')
        sim_purchases = SimulatorPackagePurchase.objects.filter(
            client=customer,
            hours_remaining__gt=0,
            package_status='active'
        ).exclude(
            gift_status='pending'
        )
        
        if sim_purchases.exists():
            total_hours = sum(float(p.hours_remaining) for p in sim_purchases)
            self.stdout.write(
                self.style.SUCCESS(
                    f'✅ {sim_purchases.count()} simulator package(s) with {total_hours:.2f} hours available'
                )
            )
            for purchase in sim_purchases:
                self.stdout.write(
                    f'   - {purchase.package.title}: '
                    f'{purchase.hours_remaining}/{purchase.hours_total} hours'
                )
        else:
            self.stdout.write(self.style.ERROR('❌ No simulator packages with available hours found'))
        
        # Check combo packages (coaching packages with simulator hours)
        self.stdout.write(f'\n🎯 Combo Packages (with Simulator Hours):')
        combo_purchases = CoachingPackagePurchase.objects.filter(
            client=customer,
            simulator_hours_remaining__gt=0,
            package_status='active'
        ).exclude(
            gift_status='pending'
        ).exclude(
            purchase_type='organization'
        )
        
        if combo_purchases.exists():
            total_hours = sum(float(p.simulator_hours_remaining) for p in combo_purchases)
            self.stdout.write(
                self.style.SUCCESS(
                    f'✅ {combo_purchases.count()} combo package(s) with {total_hours:.2f} simulator hours available'
                )
            )
            for purchase in combo_purchases:
                self.stdout.write(
                    f'   - {purchase.package.title}: '
                    f'{purchase.simulator_hours_remaining}/{purchase.simulator_hours_total} hours'
                )
        else:
            self.stdout.write(self.style.ERROR('❌ No combo packages with available simulator hours found'))
    
    def _clean_phone(self, phone):
        """Clean phone number - remove common formatting"""
        if not phone:
            return ''
        cleaned = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        return cleaned


