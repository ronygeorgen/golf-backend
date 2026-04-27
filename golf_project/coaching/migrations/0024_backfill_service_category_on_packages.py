"""
Phase C data migration: backfill service_category on CoachingPackage and SimulatorPackage.

For each package, we look for a ServiceCategory row that matches
(location_id, legacy_booking_type).  If the location-specific category is not
found we fall back to the default row (location_id='').
"""

from django.db import migrations


def _trim(raw):
    if not raw:
        return ''
    return str(raw).strip().rstrip('+').strip()


def _find_category(ServiceCategory, location_id, legacy_type):
    loc = _trim(location_id)
    if loc:
        cat = ServiceCategory.objects.filter(
            location_id=loc, legacy_booking_type=legacy_type
        ).first()
        if cat:
            return cat
    return ServiceCategory.objects.filter(
        location_id='', legacy_booking_type=legacy_type
    ).first()


def backfill(apps, schema_editor):
    CoachingPackage = apps.get_model('coaching', 'CoachingPackage')
    SimulatorPackage = apps.get_model('coaching', 'SimulatorPackage')
    ServiceCategory = apps.get_model('categories', 'ServiceCategory')

    for pkg in CoachingPackage.objects.filter(service_category__isnull=True):
        cat = _find_category(ServiceCategory, pkg.location_id, 'coaching')
        if cat:
            pkg.service_category = cat
            pkg.save(update_fields=['service_category'])

    for pkg in SimulatorPackage.objects.filter(service_category__isnull=True):
        cat = _find_category(ServiceCategory, pkg.location_id, 'simulator')
        if cat:
            pkg.service_category = cat
            pkg.save(update_fields=['service_category'])


def reverse_backfill(apps, schema_editor):
    CoachingPackage = apps.get_model('coaching', 'CoachingPackage')
    SimulatorPackage = apps.get_model('coaching', 'SimulatorPackage')
    CoachingPackage.objects.update(service_category=None)
    SimulatorPackage.objects.update(service_category=None)


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0023_add_service_category_to_packages'),
        ('categories', '0002_seed_default_service_categories'),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_backfill),
    ]
