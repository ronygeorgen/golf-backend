# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('special_events', '0007_specialevent_recurring_end_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialevent',
            name='is_private',
            field=models.BooleanField(default=False, help_text='If True, this event is private and only visible to admins. Clients cannot see or register for private events.'),
        ),
    ]



