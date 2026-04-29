"""Add category_asset FK to Booking."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0012_add_service_category_to_booking'),
        ('categories', '0003_category_assets'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='category_asset',
            field=models.ForeignKey(
                blank=True,
                help_text='The specific asset booked (e.g. Table Tennis Table 1). '
                          'For needs_staff=False assets this is the availability gate.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='bookings',
                to='categories.categoryasset',
            ),
        ),
    ]
