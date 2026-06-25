from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghl', '0005_add_invoice_fields_to_ghllocation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghllocation',
            name='refund_policy',
            field=models.TextField(
                blank=True,
                help_text='Refund / cancellation policy text shown on invoice emails.',
            ),
        ),
    ]
