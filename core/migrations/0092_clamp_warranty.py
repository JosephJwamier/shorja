"""A warranty of centuries broke every date computed from it."""
from django.db import migrations


def clamp(apps, schema_editor):
    for model in ('Item', 'Reservation'):
        apps.get_model('core', model).objects.filter(warranty_days__gt=365).update(warranty_days=365)


class Migration(migrations.Migration):
    dependencies = [('core', '0091_bounds_and_sessions')]
    operations = [migrations.RunPython(clamp, migrations.RunPython.noop)]
