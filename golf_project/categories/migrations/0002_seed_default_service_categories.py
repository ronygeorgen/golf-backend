# Generated manually for Phase A: seed simulator/coaching rows per location + defaults

from django.db import migrations


def _normalize_location_id(raw):
    if raw is None:
        return ''
    s = str(raw).strip().rstrip('+').strip()
    return s


def seed_service_categories(apps, schema_editor):
    ServiceCategory = apps.get_model('categories', 'ServiceCategory')
    CoachingPackage = apps.get_model('coaching', 'CoachingPackage')
    Simulator = apps.get_model('simulators', 'Simulator')
    SimulatorPackage = apps.get_model('coaching', 'SimulatorPackage')
    Booking = apps.get_model('bookings', 'Booking')

    def ensure_pair(location_key):
        pairs = [
            {
                'name': 'Simulator',
                'slug': 'simulator',
                'customer_label': 'Book Simulator',
                'sort_order': 0,
                'legacy_booking_type': 'simulator',
            },
            {
                'name': 'Coaching',
                'slug': 'coaching',
                'customer_label': 'Book Coaching',
                'sort_order': 1,
                'legacy_booking_type': 'coaching',
            },
        ]
        for row in pairs:
            ServiceCategory.objects.update_or_create(
                location_id=location_key,
                slug=row['slug'],
                defaults={
                    'name': row['name'],
                    'customer_label': row['customer_label'],
                    'description': '',
                    'sort_order': row['sort_order'],
                    'is_active': True,
                    'legacy_booking_type': row['legacy_booking_type'],
                },
            )

    # Fallback rows when a location has no explicit categories yet
    ensure_pair('')

    location_ids = set()
    for model in (CoachingPackage, Simulator, SimulatorPackage, Booking):
        for raw in model.objects.values_list('location_id', flat=True):
            loc = _normalize_location_id(raw)
            if loc:
                location_ids.add(loc)

    for loc in sorted(location_ids):
        ensure_pair(loc)


def unseed_service_categories(apps, schema_editor):
    ServiceCategory = apps.get_model('categories', 'ServiceCategory')
    ServiceCategory.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('categories', '0001_initial'),
        ('bookings', '0011_tempbooking_payment_id_tempbooking_processed_at_and_more'),
        ('coaching', '0022_simulatorpackagepurchase_referral_id'),
        ('simulators', '0008_simulator_location_id_alter_simulator_bay_number_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_service_categories, unseed_service_categories),
    ]
