from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0024_backfill_service_category_on_packages'),
    ]

    operations = [
        # CoachingPackage — new field
        migrations.AddField(
            model_name='coachingpackage',
            name='category_hours',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Number of asset hours included for the linked dynamic service category (e.g. table tennis table time). Set > 0 to make this a combo package.',
                max_digits=6,
            ),
        ),
        # CoachingPackagePurchase — two new fields
        migrations.AddField(
            model_name='coachingpackagepurchase',
            name='category_hours_total',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Total category asset hours included in this purchase',
                max_digits=6,
            ),
        ),
        migrations.AddField(
            model_name='coachingpackagepurchase',
            name='category_hours_remaining',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Remaining category asset hours in this purchase',
                max_digits=6,
            ),
        ),
    ]
