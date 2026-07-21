from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('admin_panel', '0005_bulkuploadtask_location_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='liabilitywaiver',
            name='location_id',
            field=models.CharField(
                blank=True,
                help_text="GHL location ID this waiver belongs to. NULL means global.",
                max_length=100,
                null=True,
            ),
        ),
    ]
