"""Give every existing item its one default variant.

Establishes the invariant the whole variant design rests on: every item has
at least one active variant, and the item's stock lives there. A plain item —
which is every item that exists before this migration — carries a single
variant with empty attributes holding all of its quantity.

Without this, every reservation, transfer and delivery path would need a
"but what if there is no variant" branch. With it, none of them do.

Reversible: dropping the variants leaves Item.quantity exactly as it was,
because it was never changed — it is the denormalised total and the seed
copies from it rather than the other way round.
"""
from django.db import migrations


def seed_default_variants(apps, schema_editor):
    Item = apps.get_model('core', 'Item')
    ItemVariant = apps.get_model('core', 'ItemVariant')

    to_create = [
        ItemVariant(item=item, attributes={}, quantity=item.quantity, is_active=True)
        for item in Item.objects.filter(variants__isnull=True)
    ]
    ItemVariant.objects.bulk_create(to_create, batch_size=500)


def drop_default_variants(apps, schema_editor):
    ItemVariant = apps.get_model('core', 'ItemVariant')
    ItemVariant.objects.filter(attributes={}).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0058_item_variants'),
    ]

    operations = [
        migrations.RunPython(seed_default_variants, drop_default_variants),
    ]
