# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('special_events', '0006_specialevent_location_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialevent',
            name='recurring_end_date',
            field=models.DateField(blank=True, help_text='End date for recurring events. Recurring occurrences will stop on this date.', null=True),
        ),
    ]

