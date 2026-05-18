"""
Add CategoryAsset and CategoryAssetAvailability models.

Each asset belongs to a ServiceCategory, has a price_per_hour, a needs_staff
flag, and location scoping.  CategoryAssetAvailability stores weekly recurring
time windows for assets that don't require a staff member (needs_staff=False).
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('categories', '0002_seed_default_service_categories'),
    ]

    operations = [
        migrations.CreateModel(
            name='CategoryAsset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text="Display name, e.g. 'Table Tennis Table 1'", max_length=120)),
                ('price_per_hour', models.DecimalField(
                    blank=True,
                    decimal_places=2,
                    help_text='Hourly rate in USD. Leave blank if not applicable.',
                    max_digits=8,
                    null=True,
                )),
                ('needs_staff', models.BooleanField(
                    default=False,
                    help_text='If True, slot availability is determined by staff schedule. '
                              'If False, availability is determined by this asset\'s own schedule.',
                )),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                ('description', models.TextField(blank=True)),
                ('location_id', models.CharField(
                    blank=True,
                    db_index=True,
                    default='',
                    help_text='GHL location id; empty = default/fallback',
                    max_length=100,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('category', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='assets',
                    to='categories.servicecategory',
                )),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='CategoryAssetAvailability',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day_of_week', models.IntegerField(choices=[
                    (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
                    (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
                ])),
                ('start_time', models.TimeField()),
                ('end_time', models.TimeField()),
                ('asset', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='availabilities',
                    to='categories.categoryasset',
                )),
            ],
            options={
                'verbose_name_plural': 'Category Asset Availabilities',
                'unique_together': {('asset', 'day_of_week', 'start_time')},
            },
        ),
    ]
