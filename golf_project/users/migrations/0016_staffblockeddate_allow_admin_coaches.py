# Generated manually — allow admin/superadmin coaches on StaffBlockedDate

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0015_alter_staffblockeddate_end_time_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffblockeddate',
            name='staff',
            field=models.ForeignKey(
                limit_choices_to={'role__in': ['staff', 'admin', 'superadmin']},
                on_delete=django.db.models.deletion.CASCADE,
                related_name='blocked_dates',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
