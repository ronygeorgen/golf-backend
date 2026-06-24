from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ghl', '0004_add_logo_to_ghllocation'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghllocation',
            name='contact_phone',
            field=models.CharField(
                blank=True,
                max_length=50,
                help_text='Location phone number shown on invoice emails (e.g. +1 902-555-0100).',
            ),
        ),
        migrations.AddField(
            model_name='ghllocation',
            name='support_email',
            field=models.EmailField(
                blank=True,
                help_text='Location support email shown on invoice emails (e.g. support@mygolfcenter.com).',
            ),
        ),
        migrations.AddField(
            model_name='ghllocation',
            name='business_id',
            field=models.CharField(
                blank=True,
                max_length=100,
                help_text='Business registration / GST-HST number shown on invoice emails.',
            ),
        ),
    ]
