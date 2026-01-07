"""
Django management command to migrate simulator package purchases from CoachingPackagePurchase 
to SimulatorPackagePurchase model.

This script:
1. Finds CoachingPackagePurchase records with purchase_name matching simulator package names
2. Finds the corresponding SimulatorPackage
3. Creates SimulatorPackagePurchase records with the same data
4. Deletes the original CoachingPackagePurchase records

Usage:
    python manage.py migrate_simulator_purchases --dry-run  # Test without making changes
    python manage.py migrate_simulator_purchases            # Actually migrate
"""

from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from coaching.models import (
    CoachingPackagePurchase, 
    SimulatorPackagePurchase, 
    SimulatorPackage
)


class Command(BaseCommand):
    help = 'Migrate simulator package purchases from CoachingPackagePurchase to SimulatorPackagePurchase'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be migrated without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        # Simulator package names to migrate
        simulator_package_names = [
            "sim only",
            "SIM+ 10",
            "SIM+ 20",
            "SIM+ 30",
            "SIM+ 50",
            "Weekday Warriors - Unlimited Sim (1 Month)"
        ]
        
        stats = {
            'found': 0,
            'migrated': 0,
            'skipped': 0,
            'errors': 0,
            'error_details': []
        }
        
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write('SIMULATOR PACKAGE PURCHASE MIGRATION')
        self.stdout.write('='*60)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made\n'))
        else:
            self.stdout.write('Migrating purchases...\n')
        
        # Process each simulator package name
        for package_name in simulator_package_names:
            self.stdout.write(f'\n📦 Processing: "{package_name}"')
            
            # Find CoachingPackagePurchase records with this purchase_name
            coaching_purchases = CoachingPackagePurchase.objects.filter(
                purchase_name=package_name
            ).select_related('client', 'package')
            
            if not coaching_purchases.exists():
                self.stdout.write(f'   No purchases found with name "{package_name}"')
                continue
            
            self.stdout.write(f'   Found {coaching_purchases.count()} purchase(s)')
            
            # Find the corresponding SimulatorPackage
            simulator_package = None
            try:
                # Try exact match first
                simulator_package = SimulatorPackage.objects.filter(
                    title=package_name,
                    is_active=True
                ).first()
                
                # Try case-insensitive if not found
                if not simulator_package:
                    simulator_package = SimulatorPackage.objects.filter(
                        title__iexact=package_name,
                        is_active=True
                    ).first()
                
                if not simulator_package:
                    self.stdout.write(
                        self.style.ERROR(
                            f'   ❌ SimulatorPackage not found for "{package_name}"'
                        )
                    )
                    stats['skipped'] += coaching_purchases.count()
                    continue
                
                self.stdout.write(f'   ✅ Found SimulatorPackage: {simulator_package.title} (ID: {simulator_package.id})')
                
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'   ❌ Error finding SimulatorPackage: {e}')
                )
                stats['errors'] += coaching_purchases.count()
                stats['error_details'].append(f'{package_name}: {str(e)}')
                continue
            
            # Process each purchase
            for coaching_purchase in coaching_purchases:
                stats['found'] += 1
                try:
                    self._migrate_purchase(
                        coaching_purchase, 
                        simulator_package, 
                        package_name,
                        dry_run,
                        stats
                    )
                except Exception as e:
                    stats['errors'] += 1
                    error_msg = f'   ❌ Error migrating purchase ID {coaching_purchase.id}: {str(e)}'
                    stats['error_details'].append(error_msg)
                    self.stdout.write(self.style.ERROR(error_msg))
        
        # Print summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write('MIGRATION SUMMARY')
        self.stdout.write('='*60)
        self.stdout.write(f'Total purchases found: {stats["found"]}')
        self.stdout.write(self.style.SUCCESS(f'Successfully migrated: {stats["migrated"]}'))
        self.stdout.write(self.style.WARNING(f'Skipped: {stats["skipped"]}'))
        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {stats["errors"]}'))
            if stats['error_details']:
                self.stdout.write('\nError Details:')
                for error in stats['error_details']:
                    self.stdout.write(self.style.ERROR(f'  - {error}'))
        self.stdout.write('='*60 + '\n')
    
    def _migrate_purchase(self, coaching_purchase, simulator_package, package_name, dry_run, stats):
        """Migrate a single purchase from CoachingPackagePurchase to SimulatorPackagePurchase"""
        client = coaching_purchase.client
        
        # Use simulator_hours from coaching purchase (since these are simulator packages)
        hours_total = coaching_purchase.simulator_hours_total or Decimal('0')
        hours_remaining = coaching_purchase.simulator_hours_remaining or Decimal('0')
        
        # If no simulator hours, skip (shouldn't happen for simulator packages)
        if hours_total == 0 and hours_remaining == 0:
            self.stdout.write(
                self.style.WARNING(
                    f'   ⚠️  Purchase ID {coaching_purchase.id}: No simulator hours found, skipping'
                )
            )
            stats['skipped'] += 1
            return
        
        if dry_run:
            self.stdout.write(
                f'   [DRY RUN] Would migrate purchase ID {coaching_purchase.id} for {client.phone}: '
                f'{hours_remaining}/{hours_total} hours'
            )
            stats['migrated'] += 1
            return
        
        # Create SimulatorPackagePurchase
        with transaction.atomic():
            # Check if already exists (avoid duplicates)
            existing = SimulatorPackagePurchase.objects.filter(
                client=client,
                package=simulator_package,
                purchase_name=package_name,
                hours_total=hours_total
            ).first()
            
            if existing:
                self.stdout.write(
                    self.style.WARNING(
                        f'   ⚠️  Purchase ID {coaching_purchase.id}: Similar purchase already exists '
                        f'(ID: {existing.id}), updating hours instead'
                    )
                )
                # Update existing purchase with remaining hours
                existing.hours_remaining = hours_remaining
                existing.package_status = coaching_purchase.package_status
                existing.notes = coaching_purchase.notes or existing.notes
                existing.save()
                
                # Delete the coaching purchase
                coaching_purchase.delete()
                stats['migrated'] += 1
                return
            
            # Create new SimulatorPackagePurchase
            simulator_purchase = SimulatorPackagePurchase.objects.create(
                client=client,
                package=simulator_package,
                purchase_name=package_name,
                hours_total=hours_total,
                hours_remaining=hours_remaining,
                notes=coaching_purchase.notes,
                purchase_type='normal',  # Default to normal (simulator packages don't have organization type)
                package_status=coaching_purchase.package_status,
                purchased_at=coaching_purchase.purchased_at,
            )
            
            # Copy gift-related fields if applicable
            if coaching_purchase.purchase_type == 'gift':
                simulator_purchase.purchase_type = 'gift'
                simulator_purchase.recipient_phone = coaching_purchase.recipient_phone
                simulator_purchase.gift_status = coaching_purchase.gift_status
                simulator_purchase.gift_token = coaching_purchase.gift_token
                simulator_purchase.original_owner = coaching_purchase.original_owner
                simulator_purchase.gift_expires_at = coaching_purchase.gift_expires_at
                simulator_purchase.save()
            
            # Update any bookings that reference this CoachingPackagePurchase
            from bookings.models import Booking
            bookings_updated = Booking.objects.filter(
                package_purchase=coaching_purchase,
                booking_type='simulator'
            ).update(
                simulator_package_purchase=simulator_purchase,
                package_purchase=None
            )
            
            if bookings_updated > 0:
                self.stdout.write(
                    f'   📝 Updated {bookings_updated} booking(s) to reference new SimulatorPackagePurchase'
                )
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'   ✅ Migrated purchase ID {coaching_purchase.id} → '
                    f'SimulatorPackagePurchase ID {simulator_purchase.id} '
                    f'({hours_remaining}/{hours_total} hours)'
                )
            )
            
            # Delete the original CoachingPackagePurchase
            coaching_purchase.delete()
            stats['migrated'] += 1

