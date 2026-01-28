# Generated migration for adding time restrictions and expiry date to simulator packages

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0015_add_referral_id_to_purchases'),
    ]

    operations = [
        # Add expiry_date to SimulatorPackage
        migrations.AddField(
            model_name='simulatorpackage',
            name='expiry_date',
            field=models.DateField(blank=True, help_text='Expiry date for this package. After this date, clients cannot use the package.', null=True),
        ),
        # Add expiry_date to SimulatorPackagePurchase
        migrations.AddField(
            model_name='simulatorpackagepurchase',
            name='expiry_date',
            field=models.DateField(blank=True, help_text='Expiry date for this purchase. After this date, the package cannot be used.', null=True),
        ),
        # Create SimulatorPackageTimeRestriction model
        migrations.CreateModel(
            name='SimulatorPackageTimeRestriction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_recurring', models.BooleanField(default=True, help_text='If True, this is a recurring restriction (day of week). If False, it\'s a specific date.')),
                ('day_of_week', models.IntegerField(blank=True, choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')], help_text='Day of week (0=Monday, 6=Sunday). Only used if is_recurring=True.', null=True)),
                ('date', models.DateField(blank=True, help_text='Specific date for non-recurring restriction. Only used if is_recurring=False.', null=True)),
                ('start_time', models.TimeField(help_text='Start time for this restriction')),
                ('end_time', models.TimeField(help_text='End time for this restriction')),
                ('limit_count', models.PositiveIntegerField(default=1, help_text='Maximum number of times this package can be used on this day/date within the time window')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='time_restrictions', to='coaching.simulatorpackage')),
            ],
            options={
                'verbose_name': 'Simulator Package Time Restriction',
                'verbose_name_plural': 'Simulator Package Time Restrictions',
                'ordering': ['package', 'is_recurring', 'day_of_week', 'date', 'start_time'],
            },
        ),
        # Add unique constraints
        migrations.AlterUniqueTogether(
            name='simulatorpackagetimerestriction',
            unique_together={('package', 'is_recurring', 'day_of_week', 'start_time'), ('package', 'is_recurring', 'date', 'start_time')},
        ),
    ]





