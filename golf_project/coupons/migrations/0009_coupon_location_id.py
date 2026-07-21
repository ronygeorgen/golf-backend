from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coupons', '0008_couponusage_item_label'),
    ]

    operations = [
        migrations.AddField(
            model_name='coupon',
            name='location_id',
            field=models.CharField(
                blank=True,
                help_text="GHL location ID this coupon belongs to. NULL means global (all locations).",
                max_length=100,
                null=True,
            ),
        ),
    ]
