from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations


def forward(apps, schema_editor):
    """The commissions' account exists before anyone can take its name; and
    reservations still waiting for payment keep the warranty they show today."""
    User = apps.get_model('core', 'User')
    username = getattr(settings, 'PLATFORM_ACCOUNT_USERNAME', 'platform')
    if not User.objects.filter(username=username).exists():
        User.objects.create(username=username, is_active=False, password=make_password(None),
                            first_name='الشورجة', last_name='المنصّة')

    Reservation = apps.get_model('core', 'Reservation')
    for reservation in Reservation.objects.filter(status='pending').select_related('item'):
        reservation.warranty_days = reservation.item.warranty_days
        reservation.save(update_fields=['warranty_days'])


class Migration(migrations.Migration):
    dependencies = [('core', '0082_offer_variant_and_payout_details')]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
