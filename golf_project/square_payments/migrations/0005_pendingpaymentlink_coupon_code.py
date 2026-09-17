from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('square_payments', '0004_quick_checkout_blocks_payment_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='pendingpaymentlink',
            name='coupon_code',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Optional coupon applied when this payment link was created.',
                max_length=50,
            ),
        ),
    ]
