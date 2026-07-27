"""
Phase 8 permission review.

Phases 5-7 built admin actions (suspend/reactivate/verify-identity a user,
toggle a store's is_active) that the Phase 0 groups were never actually
granted the underlying model permission for — change_user and
change_userstore weren't in the original mapping. Once the admin.action
permission gate was added (Phase 8, matching every other guarded action in
this admin), those actions would have silently disappeared for the teams
whose job they are:

- الدعم الفني (Support) investigates complaints/disputes and is the natural
  owner of suspending/reactivating/verifying a user's account.
- إدارة المحتوى (Content) already moderates items/reviews; deactivating a
  store selling prohibited items is the same kind of decision.

Adds the two missing permissions rather than editing 0033, which is already
applied history.
"""
from django.db import migrations

GRANTS = {
    'الدعم الفني': ['view_user', 'change_user'],
    'إدارة المحتوى': ['view_userstore', 'change_userstore'],
}


def grant_permissions(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    for group_name, codenames in GRANTS.items():
        group = Group.objects.get(name=group_name)
        perms = Permission.objects.filter(content_type__app_label='core', codename__in=codenames)
        for perm in perms:
            group.permissions.add(perm)


def revoke_permissions(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    for group_name, codenames in GRANTS.items():
        group = Group.objects.get(name=group_name)
        perms = Permission.objects.filter(content_type__app_label='core', codename__in=codenames)
        for perm in perms:
            group.permissions.remove(perm)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0037_item_admin_notes_user_admin_notes'),
    ]

    operations = [
        migrations.RunPython(grant_permissions, revoke_permissions),
    ]
