"""
Creates the fixed "support" identity used as Chat.seller for every user's
"التواصل مع الدعم" conversation (core/services/support.py). It's a system
placeholder, not a real login: password is unusable, is_staff=False since
nobody signs into it directly — staff reply through the admin's dedicated
Chat reply view instead (core/admin.py ChatAdmin.reply_view), which records
the *actual* staff member as Message.sender while the chat's counterparty
identity stays this one fixed account.

password is set via make_password(None) rather than the model's own
set_unusable_password() — the historical model this migration operates on
doesn't reliably carry AbstractUser's convenience methods, only its fields.
"""
from django.contrib.auth.hashers import make_password
from django.db import migrations

SUPPORT_USERNAME = 'الدعم_الفني'


def create_support_account(apps, schema_editor):
    User = apps.get_model('core', 'User')
    if not User.objects.filter(username=SUPPORT_USERNAME).exists():
        User.objects.create(
            username=SUPPORT_USERNAME,
            password=make_password(None),
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )


def delete_support_account(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.filter(username=SUPPORT_USERNAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0044_auction_live_session'),
    ]

    operations = [
        migrations.RunPython(create_support_account, delete_support_account),
    ]
