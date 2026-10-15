"""
Phase 8 — permission matrix.

Confirms the fix for a real gap this phase found: Django admin bulk actions
are NOT permission-gated unless the @admin.action decorator explicitly
declares permissions=[...] — every custom action in this codebase lacked
that until now, so any staff account with mere view access to a changelist
could have run suspend_users_action, archive_selected_items, etc. regardless
of their group. These tests exercise the real POST path for each of the
four Phase 0 groups against each guarded action and assert the *state
doesn't change* for a group that shouldn't be able to perform it — not just
that a permission string is present, which wouldn't have caught the actual
bug (the action ran fine with no permission check at all).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Category, Dispute, Item, Reservation, User, UserStore, WithdrawalRequest,
    get_or_create_wallet,
)

pytestmark = pytest.mark.django_db

GROUPS = ('الدعم الفني', 'الشؤون المالية', 'إدارة المحتوى', 'عمليات التوصيل')


def make_staff(username, group_name):
    user = User.objects.create_user(username=username, password='pass12345', is_staff=True)
    user.groups.add(Group.objects.get(name=group_name))
    return user


def make_dispute():
    owner, buyer = User.objects.create_user(username=f'pm_owner_{id(object())}', password='x'), \
        User.objects.create_user(username=f'pm_buyer_{id(object())}', password='x')
    category, _ = Category.objects.get_or_create(name='pm-cat', slug='pm-cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=1, status='active')
    return Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='t', description='d')


def make_withdrawal():
    user = User.objects.create_user(username=f'pm_wd_{id(object())}', password='x')
    wallet = get_or_create_wallet(user)
    wallet.credit(Decimal('50'))
    wallet.hold(Decimal('50'))
    return WithdrawalRequest.objects.create(user=user, amount=Decimal('50'), status='pending')


def make_item():
    owner = User.objects.create_user(username=f'pm_item_owner_{id(object())}', password='x')
    category, _ = Category.objects.get_or_create(name='pm-cat2', slug='pm-cat2')
    return Item.objects.create(owner=owner, title='i2', description='d', category=category,
                                condition='used', quantity=1, status='active')


def make_target_user():
    return User.objects.create_user(username=f'pm_target_{id(object())}', password='x')


def make_store():
    owner = User.objects.create_user(username=f'pm_store_owner_{id(object())}', password='x')
    return UserStore.objects.create(user=owner, name='s', is_active=True)


# ---------------------------------------------------------------------------
# dispute resolution — only Support (+ superuser) should be able to resolve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,should_succeed', [
    ('الدعم الفني', True),
    ('الشؤون المالية', False),
    ('إدارة المحتوى', False),
    ('عمليات التوصيل', False),
])
def test_dispute_resolve_view_is_gated_by_group(group_name, should_succeed):
    dispute = make_dispute()
    staff = make_staff(f'pm_disp_{group_name}', group_name)

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_dispute_resolve', args=[dispute.pk]), {
        'resolution_action': 'no_action', 'resolution': 'x', 'admin_notes': '',
    })

    dispute.refresh_from_db()
    if should_succeed:
        assert response.status_code == 302
        assert dispute.status == 'resolved'
    else:
        assert response.status_code == 403
        assert dispute.status == 'open'


# ---------------------------------------------------------------------------
# withdrawal review — only Finance (+ superuser) should be able to approve
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,should_succeed', [
    ('الدعم الفني', False),
    ('الشؤون المالية', True),
    ('إدارة المحتوى', False),
    ('عمليات التوصيل', False),
])
def test_withdrawal_review_is_gated_by_group(group_name, should_succeed):
    withdrawal = make_withdrawal()
    staff = make_staff(f'pm_wd_{group_name}', group_name)

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_withdrawalrequest_review', args=[withdrawal.pk]), {
        'decision': 'approve', 'admin_notes': '',
    })

    withdrawal.refresh_from_db()
    if should_succeed:
        assert response.status_code == 302
        assert withdrawal.status == 'approved'
    else:
        assert response.status_code == 403
        assert withdrawal.status == 'pending'


# ---------------------------------------------------------------------------
# item archiving — only Content (+ superuser) should be able to archive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,should_succeed', [
    ('الدعم الفني', False),
    ('الشؤون المالية', False),
    ('إدارة المحتوى', True),
    ('عمليات التوصيل', False),
])
def test_item_archive_action_is_gated_by_group(group_name, should_succeed):
    item = make_item()
    staff = make_staff(f'pm_item_{group_name}', group_name)

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_item_changelist'), {
        'action': 'archive_selected_items',
        '_selected_action': [item.pk],
        'apply': '1',
        'reason': 'مخالفة',
    })

    item.refresh_from_db()
    if should_succeed:
        assert item.status == 'archived'
    else:
        # A group without change_item never sees the action succeed — it's
        # either filtered out of get_actions() or falls through unapplied.
        assert item.status == 'active'


# ---------------------------------------------------------------------------
# user suspension — only Support (+ superuser) should be able to suspend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,should_succeed', [
    ('الدعم الفني', True),
    ('الشؤون المالية', False),
    ('إدارة المحتوى', False),
    ('عمليات التوصيل', False),
])
def test_user_suspend_action_is_gated_by_group(group_name, should_succeed):
    target = make_target_user()
    staff = make_staff(f'pm_sus_{group_name}', group_name)

    client = Client()
    client.force_login(staff)
    client.post(reverse('admin:core_user_changelist'), {
        'action': 'suspend_users_action',
        '_selected_action': [target.pk],
        'apply': '1',
        'reason': 'اختبار',
    })

    target.refresh_from_db()
    assert target.is_active is (not should_succeed)


# ---------------------------------------------------------------------------
# store activation toggle — only Content (+ superuser) should be able to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,should_succeed', [
    ('الدعم الفني', False),
    ('الشؤون المالية', False),
    ('إدارة المحتوى', True),
    ('عمليات التوصيل', False),
])
def test_store_deactivate_action_is_gated_by_group(group_name, should_succeed):
    store = make_store()
    staff = make_staff(f'pm_store_{group_name}', group_name)

    client = Client()
    client.force_login(staff)
    client.post(reverse('admin:core_userstore_changelist'), {
        'action': 'deactivate_stores_action',
        '_selected_action': [store.pk],
    })

    store.refresh_from_db()
    assert store.is_active is (not should_succeed)


# ---------------------------------------------------------------------------
# superuser bypasses every group boundary
# ---------------------------------------------------------------------------

def test_superuser_can_perform_every_guarded_action():
    dispute = make_dispute()
    withdrawal = make_withdrawal()
    item = make_item()
    target = make_target_user()
    store = make_store()
    superuser = User.objects.create_superuser(username='pm_super', password='x', email='s@example.com')

    client = Client()
    client.force_login(superuser)

    client.post(reverse('admin:core_dispute_resolve', args=[dispute.pk]),
                {'resolution_action': 'no_action', 'resolution': 'x', 'admin_notes': ''})
    client.post(reverse('admin:core_withdrawalrequest_review', args=[withdrawal.pk]),
                {'decision': 'approve', 'admin_notes': ''})
    client.post(reverse('admin:core_item_changelist'),
                {'action': 'archive_selected_items', '_selected_action': [item.pk], 'apply': '1', 'reason': 'x'})
    client.post(reverse('admin:core_user_changelist'),
                {'action': 'suspend_users_action', '_selected_action': [target.pk], 'apply': '1', 'reason': 'x'})
    client.post(reverse('admin:core_userstore_changelist'),
                {'action': 'deactivate_stores_action', '_selected_action': [store.pk]})

    dispute.refresh_from_db()
    withdrawal.refresh_from_db()
    item.refresh_from_db()
    target.refresh_from_db()
    store.refresh_from_db()

    assert dispute.status == 'resolved'
    assert withdrawal.status == 'approved'
    assert item.status == 'archived'
    assert target.is_active is False
    assert store.is_active is False
