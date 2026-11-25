"""Usernames made from the phone were public and showed everyone's number.

Nobody types one to sign in (the sign-in box takes the phone), so they are
replaced. Staff and delivery companies keep theirs: those are typed.
"""
import re
import secrets

from django.db import migrations

MADE_FROM_PHONE = re.compile(r'u964\d{10}(_\d+)?$')
DIGITS = re.compile(r'user\d{7,}$')


def rename(apps, schema_editor):
    User = apps.get_model('core', 'User')
    companies = set(apps.get_model('core', 'DeliveryCompany').objects.values_list('account_id', flat=True))
    for user in User.objects.filter(username__startswith='u', is_staff=False).only('id', 'username').iterator():
        if user.pk in companies or not (MADE_FROM_PHONE.match(user.username) or DIGITS.match(user.username)):
            continue
        while True:
            name = 'user_' + ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(8))
            if not User.objects.filter(username=name).exists():
                break
        User.objects.filter(pk=user.pk).update(username=name)


class Migration(migrations.Migration):
    dependencies = [('core', '0087_order_snapshot_and_topups')]
    operations = [migrations.RunPython(rename, migrations.RunPython.noop)]
