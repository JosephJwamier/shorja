"""Representative orders still open when this ships have already taken their stock."""
from django.db import migrations


def mark(apps, schema_editor):
    apps.get_model('core', 'Reservation').objects.filter(
        status='pending', representative__isnull=False).update(stock_held=True)


class Migration(migrations.Migration):
    dependencies = [('core', '0094_dispute_replies_and_rep_stock')]
    operations = [migrations.RunPython(mark, migrations.RunPython.noop)]
