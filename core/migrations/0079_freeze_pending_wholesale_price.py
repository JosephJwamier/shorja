from django.db import migrations


def freeze(apps, schema_editor):
    """Requests still waiting get today's price as the agreed one, so the
    owner cannot raise it before approving them."""
    ItemTransfer = apps.get_model('core', 'ItemTransfer')
    FixedPriceListing = apps.get_model('core', 'FixedPriceListing')
    for transfer in ItemTransfer.objects.filter(
            status='pending', wholesale_price_per_unit__isnull=True).select_related('variant'):
        price = transfer.variant.price if transfer.variant else None
        if price is None:
            listing = FixedPriceListing.objects.filter(item_id=transfer.item_id).first()
            price = listing.price if listing else None
        if price is not None:
            transfer.wholesale_price_per_unit = price
            transfer.save(update_fields=['wholesale_price_per_unit'])


class Migration(migrations.Migration):
    dependencies = [('core', '0078_wholesale_commission')]
    operations = [migrations.RunPython(freeze, migrations.RunPython.noop)]
