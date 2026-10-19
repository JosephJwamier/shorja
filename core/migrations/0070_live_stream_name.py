"""Rename LiveSession.agora_channel_name to stream_name.

Live video moves off Agora onto our own SRS box (see infra/live/). The
column always held exactly what a media server calls a stream name — a
random `live_<hex>` — so the values carry over untouched; only the name
stops referring to a vendor we no longer use.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0069_alter_reel_options'),
    ]

    operations = [
        migrations.RenameField(
            model_name='livesession',
            old_name='agora_channel_name',
            new_name='stream_name',
        ),
    ]
