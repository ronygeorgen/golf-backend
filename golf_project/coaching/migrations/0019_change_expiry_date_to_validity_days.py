# Generated migration for changing expiry_date to validity_days in SimulatorPackage

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0018_rename_limit_count_to_limit_hours'),
    ]

    operations = [
        # Add validity_days field to SimulatorPackage
        migrations.AddField(
            model_name='simulatorpackage',
            name='validity_days',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Number of days from purchase date that this package is valid. If set, clients cannot use the package after this period.',
                null=True
            ),
        ),
        # Remove expiry_date field from SimulatorPackage (keep it in SimulatorPackagePurchase)
        migrations.RemoveField(
            model_name='simulatorpackage',
            name='expiry_date',
        ),
    ]


