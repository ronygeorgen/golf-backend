from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghl', '0003_add_timezone_to_ghllocation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghllocation',
            name='logo',
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to='location_logos/',
                help_text='Company logo (912×273 recommended, max 1 MB)'
            ),
        ),
    ]
