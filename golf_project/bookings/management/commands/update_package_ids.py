"""
Django management command to update package_id in CoachingPackagePurchase records
based on their purchase_name.

This script updates the package foreign key to point to the correct CoachingPackage
based on the purchase_name matching.

Usage:
    python manage.py update_package_ids --dry-run  # Test without making changes
    python manage.py update_package_ids            # Actually update
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from coaching.models import CoachingPackagePurchase, CoachingPackage


class Command(BaseCommand):
    help = 'Update package_id in CoachingPackagePurchase based on purchase_name'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        # Mapping: purchase_name -> package_id
        package_mapping = {
            "5 Coaching Sessions": [6, 14],  # Two package IDs for same name
            "10 Coaching Sessions": [7, 16],  # Two package IDs for same name
            "Membership": [8],
            "Test Package": [10],
            "TPI Assessment & Swing Analysis": [15],
            "20 Coaching Sessions": [17],
            "Membership - Basic": [18],
            "Membership - Performance": [19],
            "Legacy Import Package": [20],
        }
        
        stats = {
            'found': 0,
            'updated': 0,
            'skipped': 0,
            'errors': 0,
            'error_details': []
        }
        
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write('UPDATE PACKAGE IDs IN COACHING PACKAGE PURCHASES')
        self.stdout.write('='*60)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made\n'))
        else:
            self.stdout.write('Updating purchases...\n')
        
        # Process each purchase_name mapping
        for purchase_name, package_ids in package_mapping.items():
            self.stdout.write(f'\n📦 Processing: "{purchase_name}"')
            
            # Find CoachingPackagePurchase records with this purchase_name
            purchases = CoachingPackagePurchase.objects.filter(
                purchase_name=purchase_name
            ).select_related('package', 'client')
            
            if not purchases.exists():
                self.stdout.write(f'   No purchases found with name "{purchase_name}"')
                continue
            
            self.stdout.write(f'   Found {purchases.count()} purchase(s)')
            
            # Handle multiple package IDs for same name
            if len(package_ids) > 1:
                self.stdout.write(
                    self.style.WARNING(
                        f'   ⚠️  Multiple package IDs available: {package_ids}'
                    )
                )
                self.stdout.write(
                    f'   Will use first available package or match by location_id if possible'
                )
            
            # Process each purchase
            for purchase in purchases:
                stats['found'] += 1
                try:
                    self._update_purchase_package(
                        purchase, 
                        package_ids, 
                        purchase_name,
                        dry_run,
                        stats
                    )
                except Exception as e:
                    stats['errors'] += 1
                    error_msg = f'   ❌ Error updating purchase ID {purchase.id}: {str(e)}'
                    stats['error_details'].append(error_msg)
                    self.stdout.write(self.style.ERROR(error_msg))
        
        # Print summary
        self.stdout.write(self.style.SUCCESS('\n' + '='*60))
        self.stdout.write('UPDATE SUMMARY')
        self.stdout.write('='*60)
        self.stdout.write(f'Total purchases found: {stats["found"]}')
        self.stdout.write(self.style.SUCCESS(f'Successfully updated: {stats["updated"]}'))
        self.stdout.write(self.style.WARNING(f'Skipped: {stats["skipped"]}'))
        if stats['errors'] > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {stats["errors"]}'))
            if stats['error_details']:
                self.stdout.write('\nError Details:')
                for error in stats['error_details']:
                    self.stdout.write(self.style.ERROR(f'  - {error}'))
        self.stdout.write('='*60 + '\n')
    
    def _update_purchase_package(self, purchase, package_ids, purchase_name, dry_run, stats):
        """Update the package_id for a single purchase"""
        current_package = purchase.package
        current_package_id = current_package.id if current_package else None
        
        # Check if already correct
        if current_package_id in package_ids:
            self.stdout.write(
                f'   ✓ Purchase ID {purchase.id}: Already has correct package_id {current_package_id}'
            )
            stats['skipped'] += 1
            return
        
        # Try to find the best matching package
        target_package = None
        
        # If multiple package IDs, try to match by location_id if purchase has one
        if len(package_ids) > 1:
            # Check if we can match by location_id
            # First, check if the purchase's client has a location_id
            client_location = getattr(purchase.client, 'ghl_location_id', None)
            
            # Try each package ID
            for package_id in package_ids:
                try:
                    pkg = CoachingPackage.objects.get(id=package_id, is_active=True)
                    # If client has location_id, try to match
                    if client_location and pkg.location_id == client_location:
                        target_package = pkg
                        break
                    # If no location match but first package, use it
                    if not target_package:
                        target_package = pkg
                except CoachingPackage.DoesNotExist:
                    continue
        else:
            # Single package ID
            try:
                target_package = CoachingPackage.objects.get(id=package_ids[0], is_active=True)
            except CoachingPackage.DoesNotExist:
                raise ValueError(f'Package with ID {package_ids[0]} not found or not active')
        
        if not target_package:
            raise ValueError(f'No valid package found from IDs: {package_ids}')
        
        if dry_run:
            self.stdout.write(
                f'   [DRY RUN] Would update purchase ID {purchase.id} '
                f'(customer: {purchase.client.phone}): '
                f'package_id {current_package_id} → {target_package.id} '
                f'({current_package.title if current_package else "None"} → {target_package.title})'
            )
            stats['updated'] += 1
            return
        
        # Update the purchase
        with transaction.atomic():
            old_package_title = current_package.title if current_package else "None"
            purchase.package = target_package
            purchase.save(update_fields=['package', 'updated_at'])
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'   ✅ Updated purchase ID {purchase.id} '
                    f'(customer: {purchase.client.phone}): '
                    f'package_id {current_package_id} → {target_package.id} '
                    f'({old_package_title} → {target_package.title})'
                )
            )
            stats['updated'] += 1


