"""Held wholesale money is decided by finance now, so they must see it."""
from django.db import migrations


def grant(apps, schema_editor):
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config('core'), verbosity=0)
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    group = Group.objects.filter(name='الشؤون المالية').first()
    if group is not None:
        group.permissions.add(*Permission.objects.filter(
            content_type__app_label='core', codename='view_itemtransfer'))


class Migration(migrations.Migration):
    dependencies = [('core', '0089_blocking_and_offer_expiry')]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
