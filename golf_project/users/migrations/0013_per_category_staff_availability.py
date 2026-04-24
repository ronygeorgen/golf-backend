"""
Per-category staff availability.

Adds an optional service_category FK to StaffAvailability, StaffDayAvailability,
and StaffBlockedDate.  NULL means "general" (applies to all categories), which
preserves full backward compatibility — all existing rows keep their current
behaviour because their service_category will remain NULL after this migration.

unique_together constraints are updated to include service_category so that the
same time-window can exist once as general (NULL) and once per category.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_add_staff_category'),
        ('categories', '0002_seed_default_service_categories'),
    ]

    operations = [
        # ── StaffAvailability ──────────────────────────────────────────────
        migrations.AddField(
            model_name='staffavailability',
            name='service_category',
            field=models.ForeignKey(
                blank=True,
                help_text='If set, this availability applies only to this service category. '
                          'Leave blank for general availability (applies to all categories).',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='staff_availabilities',
                to='categories.servicecategory',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='staffavailability',
            unique_together={('staff', 'day_of_week', 'start_time', 'service_category')},
        ),

        # ── StaffDayAvailability ───────────────────────────────────────────
        migrations.AddField(
            model_name='staffdayavailability',
            name='service_category',
            field=models.ForeignKey(
                blank=True,
                help_text='If set, this day-specific availability applies only to this service category. '
                          'Leave blank for general availability.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='staff_day_availabilities',
                to='categories.servicecategory',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='staffdayavailability',
            unique_together={('staff', 'date', 'start_time', 'service_category')},
        ),

        # ── StaffBlockedDate ───────────────────────────────────────────────
        migrations.AddField(
            model_name='staffblockeddate',
            name='service_category',
            field=models.ForeignKey(
                blank=True,
                help_text='If set, this block applies only to this service category. '
                          'Leave blank to block across all categories.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='staff_blocked_dates',
                to='categories.servicecategory',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='staffblockeddate',
            unique_together={('staff', 'date', 'start_time', 'end_time', 'service_category')},
        ),
    ]
