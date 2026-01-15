# Generated migration for adding race condition prevention

from django.db import migrations, models
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0001_initial'),  # Update this to your latest migration
    ]

    operations = [
        # Add status field to TempBooking
        migrations.AddField(
            model_name='tempbooking',
            name='status',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('reserved', 'Reserved'),
                    ('completed', 'Completed'),
                    ('expired', 'Expired'),
                    ('cancelled', 'Cancelled'),
                ],
                default='reserved',
                help_text='Status of the temporary booking reservation'
            ),
        ),
        # Add payment_id for idempotency
        migrations.AddField(
            model_name='tempbooking',
            name='payment_id',
            field=models.CharField(
                max_length=255,
                null=True,
                blank=True,
                unique=True,
                help_text='External payment ID for idempotency'
            ),
        ),
        # Add processed_at timestamp
        migrations.AddField(
            model_name='tempbooking',
            name='processed_at',
            field=models.DateTimeField(
                null=True,
                blank=True,
                help_text='When the temp booking was converted to a real booking'
            ),
        ),
        # Add index on status and expires_at for cleanup queries
        migrations.AddIndex(
            model_name='tempbooking',
            index=models.Index(
                fields=['status', 'expires_at'],
                name='tempbook_status_expires_idx'
            ),
        ),
        # Add index on payment_id for idempotency checks
        migrations.AddIndex(
            model_name='tempbooking',
            index=models.Index(
                fields=['payment_id'],
                name='tempbook_payment_id_idx',
                condition=models.Q(payment_id__isnull=False)
            ),
        ),
        # Add composite index for availability checks
        migrations.AddIndex(
            model_name='tempbooking',
            index=models.Index(
                fields=['simulator', 'start_time', 'end_time', 'status'],
                name='tempbook_avail_check_idx'
            ),
        ),
        # Note: PostgreSQL exclusion constraint for preventing overlaps
        # This requires the btree_gist extension
        # If using PostgreSQL, uncomment the following:
        
        # migrations.RunSQL(
        #     sql='CREATE EXTENSION IF NOT EXISTS btree_gist;',
        #     reverse_sql='-- Extension btree_gist is shared, do not drop'
        # ),
        
        # Add exclusion constraint to prevent overlapping bookings
        # This is a database-level guarantee that no two confirmed/completed bookings
        # can overlap for the same simulator
        
        # For PostgreSQL with btree_gist extension:
        # migrations.AddConstraint(
        #     model_name='booking',
        #     constraint=ExclusionConstraint(
        #         name='prevent_simulator_overlap',
        #         expressions=[
        #             ('simulator_id', RangeOperators.EQUAL),
        #             (
        #                 "tstzrange(start_time, end_time, '[]')",
        #                 RangeOperators.OVERLAPS
        #             ),
        #         ],
        #         condition=models.Q(
        #             booking_type='simulator',
        #             status__in=['confirmed', 'completed']
        #         ),
        #     ),
        # ),
        
        # For databases without exclusion constraints, add a unique constraint
        # that helps catch some conflicts (not perfect but better than nothing)
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(
                fields=['simulator', 'start_time', 'booking_type', 'status'],
                name='booking_conflict_check_idx'
            ),
        ),
    ]
