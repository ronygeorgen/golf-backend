# Generated migration for adding referral_id to purchases

from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('coaching', '0014_temppurchase_package_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='temppurchase',
            name='referral_id',
            field=models.ForeignKey(
                blank=True,
                help_text="Staff member who referred this purchase (optional)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='referred_temp_purchases',
                to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name='coachingpackagepurchase',
            name='referral_id',
            field=models.ForeignKey(
                blank=True,
                help_text="Staff member who referred this purchase (optional)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='referred_purchases',
                to=settings.AUTH_USER_MODEL
            ),
        ),
    ]

