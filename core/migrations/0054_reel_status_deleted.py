from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the 'deleted' choice to Reel.status.

    Choices-only change (no column alteration), needed by
    ReelViewSet.destroy(): a reported reel can't be hard-deleted without
    CASCADE-wiping its Report rows, so it keeps a tombstone row in this
    status with its S3 keys blanked instead.
    """

    dependencies = [
        ('core', '0053_reel_reelcomment_reellike_reelview_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reel',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending_upload', 'Pending Upload'),
                    ('published', 'Published'),
                    ('failed', 'Failed'),
                    ('archived', 'Archived'),
                    ('deleted', 'Deleted'),
                ],
                default='pending_upload',
                max_length=20,
            ),
        ),
    ]
