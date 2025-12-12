# Generated manually for multiple simulator booking feature

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0005_tempbooking'),
    ]

    operations = [
        migrations.AddField(
            model_name='tempbooking',
            name='simulator_count',
            field=models.IntegerField(default=1, help_text='Number of simulators to book'),
        ),
    ]


