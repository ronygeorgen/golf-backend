# Generated migration for adding SimulatorPackageUsage model

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0016_add_time_restrictions_and_expiry'),
        ('bookings', '0004_booking_simulator_package_purchase'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimulatorPackageUsage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('usage_date', models.DateField(help_text='Date when the package was used')),
                ('usage_time', models.TimeField(help_text='Time when the package was used')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('booking', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='package_usage_records', to='bookings.booking')),
                ('package_purchase', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usage_records', to='coaching.simulatorpackagepurchase')),
                ('restriction', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='usage_records', to='coaching.simulatorpackagetimerestriction')),
            ],
            options={
                'verbose_name': 'Simulator Package Usage',
                'verbose_name_plural': 'Simulator Package Usages',
                'ordering': ['-usage_date', '-usage_time'],
            },
        ),
        migrations.AddIndex(
            model_name='simulatorpackageusage',
            index=models.Index(fields=['package_purchase', 'usage_date', 'restriction'], name='coaching_si_package_6a8b3d_idx'),
        ),
    ]


