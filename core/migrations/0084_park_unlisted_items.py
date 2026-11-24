from django.db import migrations


def park(apps, schema_editor):
    """Products left 'active' by a stream or auction that ended, with nothing
    selling them: to draft, where their seller can list them."""
    Item = apps.get_model('core', 'Item')
    AuctionListing = apps.get_model('core', 'AuctionListing')
    LiveSessionItem = apps.get_model('core', 'LiveSessionItem')
    (Item.objects.filter(status='active', fixed_listing__isnull=True)
     .exclude(pk__in=AuctionListing.objects.filter(
         status__in=['active', 'upcoming']).values('item_id'))
     .exclude(pk__in=LiveSessionItem.objects.filter(
         session__status='live', status__in=['queued', 'now_showing']).values('item_id'))
     .update(status='draft'))


class Migration(migrations.Migration):
    dependencies = [('core', '0083_platform_account')]
    operations = [migrations.RunPython(park, migrations.RunPython.noop)]
