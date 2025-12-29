# Generated migration for renaming limit_count to limit_hours and changing field type

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coaching', '0017_simulatorpackageusage'),
    ]

    operations = [
        # Rename limit_count to limit_hours and change to DecimalField
        migrations.RenameField(
            model_name='simulatorpackagetimerestriction',
            old_name='limit_count',
            new_name='limit_hours',
        ),
        migrations.AlterField(
            model_name='simulatorpackagetimerestriction',
            name='limit_hours',
            field=models.DecimalField(
                decimal_places=2,
                default=1.0,
                help_text='Maximum number of hours this package can be used on this day/date within the time window',
                max_digits=6
            ),
        ),
        # Add hours_used field to SimulatorPackageUsage
        # First add the field with a default
        migrations.AddField(
            model_name='simulatorpackageusage',
            name='hours_used',
            field=models.DecimalField(
                decimal_places=2,
                help_text='Number of hours used in this booking',
                max_digits=6,
                default=0.0
            ),
        ),
        # Then update existing records to calculate hours from booking duration
        migrations.RunPython(
            code=lambda apps, schema_editor: None,  # No data migration needed for new field
            reverse_code=migrations.RunPython.noop,
        ),
    ]

