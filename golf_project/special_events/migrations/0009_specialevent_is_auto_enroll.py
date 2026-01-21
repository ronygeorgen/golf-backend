# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('special_events', '0008_specialevent_is_private'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialevent',
            name='is_auto_enroll',
            field=models.BooleanField(default=False, help_text='If True, registered customers will be automatically enrolled for the next occurrence. Only applicable for weekly and monthly recurring events.'),
        ),
    ]




