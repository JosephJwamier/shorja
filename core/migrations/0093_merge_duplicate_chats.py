"""Store and support chats opened twice by a double tap: one conversation again."""
from django.db import migrations
from django.db.models import Count, Min


def merge(apps, schema_editor):
    Chat = apps.get_model('core', 'Chat')
    Message = apps.get_model('core', 'Message')
    groups = (Chat.objects.filter(item__isnull=True).order_by()
              .values('buyer', 'seller', 'store').annotate(n=Count('id'), keep=Min('id')).filter(n__gt=1))
    for g in groups:
        extra = list(Chat.objects.filter(
            item__isnull=True, buyer=g['buyer'], seller=g['seller'], store=g['store'],
        ).exclude(pk=g['keep']).values_list('pk', flat=True))
        Message.objects.filter(chat_id__in=extra).update(chat_id=g['keep'])
        Chat.objects.filter(pk__in=extra).delete()


class Migration(migrations.Migration):
    dependencies = [('core', '0092_clamp_warranty')]
    operations = [migrations.RunPython(merge, migrations.RunPython.noop)]
