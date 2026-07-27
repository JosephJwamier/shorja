"""
Phase 0 — scoped admin roles.

Replaces "any is_staff account can do anything in /admin/" with four groups,
each holding only the model permissions that role actually needs. A group
membership only grants Django's built-in view/add/change/delete on a model;
money-moving actions (resolving a dispute, approving a withdrawal) stay
gated in code, not by this alone — see Phase 3 / Phase 5.

is_superuser is intentionally not modeled as a group: Django already grants
superusers every permission regardless of group membership.
"""
from django.db import migrations

GROUPS = {
    'الدعم الفني': {
        'view': [
            'user', 'userstore', 'location', 'item', 'reservation',
            'chat', 'message', 'rating', 'productreview', 'storereview',
            'notification',
        ],
        'change': ['dispute'],
    },
    'الشؤون المالية': {
        'view': ['reservation', 'user'],
        'change': [
            'transaction', 'wallet', 'depositrequest', 'withdrawalrequest',
            'storagefeepayment', 'representative',
        ],
    },
    'إدارة المحتوى': {
        'view': ['userstore'],
        'change': [
            'item', 'itemimage', 'category', 'fixedpricelisting',
            'auctionlisting', 'bid', 'productreview', 'storereview',
            'rating', 'livesession', 'livesessionitem',
            'livesessioncomment', 'livesessionlike', 'storecoupon',
            'couponcollection',
        ],
        'delete': [
            'item', 'productreview', 'storereview', 'livesessioncomment',
            'storecoupon',
        ],
    },
    'عمليات التوصيل': {
        'view': ['reservation', 'location'],
        'change': ['deliverycompany', 'deliveryorder', 'itemtransfer'],
    },
}


def codenames_for(role):
    """view: view-only. change: view+add+change. delete: also delete."""
    names = set()
    for model in role.get('view', []):
        names.add(f'view_{model}')
    for model in role.get('change', []):
        names.update({f'view_{model}', f'add_{model}', f'change_{model}'})
    for model in role.get('delete', []):
        names.add(f'delete_{model}')
    return names


def create_groups(apps, schema_editor):
    # Permission rows are normally created by Django's post_migrate signal,
    # which only fires once the *entire* migrate run finishes — too late for
    # a data migration inside this same app's migration sequence. On a
    # from-scratch database (fresh clone, CI, `manage.py test`) the 'core'
    # permissions this migration needs don't exist yet without this.
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    create_permissions(global_apps.get_app_config('core'), verbosity=0)

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    for group_name, role in GROUPS.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        codenames = codenames_for(role)
        perms = list(
            Permission.objects.filter(
                content_type__app_label='core', codename__in=codenames
            )
        )
        group.permissions.set(perms)


def delete_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=GROUPS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_category_image_alter_category_icon'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(create_groups, delete_groups),
    ]
