from django.db import migrations, models


def mark_existing_qc_coupons(apps, schema_editor):
    Coupon = apps.get_model('coupons', 'Coupon')
    Coupon.objects.filter(
        description__startswith='Quick Checkout custom discount',
    ).update(quick_checkout_only=True)


class Migration(migrations.Migration):

    dependencies = [
        ('coupons', '0009_coupon_location_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='coupon',
            name='quick_checkout_only',
            field=models.BooleanField(
                default=False,
                help_text='If true, coupon can only be applied from staff Quick Checkout.',
            ),
        ),
        migrations.RunPython(mark_existing_qc_coupons, migrations.RunPython.noop),
    ]
