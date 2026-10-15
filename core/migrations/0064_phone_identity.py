"""The phone becomes the account's identity: unique, canonical, NULL when absent.

The data step has to run before the constraints, not after. Today every
phone-less row holds '' and every email-less row holds '', and MySQL permits
exactly one '' in a unique index — so adding UNIQUE to a table with two such
rows fails outright. Collapsing blanks to NULL first makes the constraint
addable, since NULLs do not collide.

Existing numbers are rewritten to E.164 in the same pass. Anything that
cannot be parsed is set to NULL rather than carried over: an unparseable
number can neither receive an SMS nor be matched at login, so keeping it
would only let it collide with the real number when its owner re-enters it
properly.
"""
import core.models
from django.db import migrations, models


def canonicalise(apps, schema_editor):
    from core.phone_utils import to_e164

    User = apps.get_model('core', 'User')
    # .update() per row, not .save(): the historical model in a migration has
    # no custom save(), and we want the raw rewrite anyway.
    for user in User.objects.all().only('id', 'phone', 'email'):
        phone = to_e164(user.phone) or None
        email = (user.email or '').strip().lower() or None
        if phone != user.phone or email != user.email:
            User.objects.filter(pk=user.pk).update(phone=phone, email=email)


def back_to_blanks(apps, schema_editor):
    """Reverse: NULL -> '' so the columns can go back to NOT NULL. The
    canonical form is kept — reversing the constraint does not require
    reversing the normalisation, and guessing the original spelling is
    impossible anyway."""
    User = apps.get_model('core', 'User')
    User.objects.filter(phone__isnull=True).update(phone='')
    User.objects.filter(email__isnull=True).update(email='')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0063_delivery_platform_commission'),
    ]

    operations = [
        # 1. Widen to NULL-able first so the data step has somewhere to put
        #    the blanks, but without UNIQUE yet — duplicates may still exist.
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(blank=True, max_length=254, null=True, verbose_name='email address'),
        ),
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        # 2. Collapse blanks to NULL and rewrite numbers to E.164.
        migrations.RunPython(canonicalise, back_to_blanks),
        # 3. Now the unique indexes can be built.
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True, verbose_name='email address'),
        ),
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(blank=True, help_text='E.164, normalised on save (+9647XXXXXXXXX)', max_length=20, null=True, unique=True, validators=[core.models.validate_iraqi_mobile]),
        ),
    ]
