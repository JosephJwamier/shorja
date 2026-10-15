"""
Phase 6 — the admin moderation actions themselves (bulk suspend/archive
flows, identity verification, store toggling, live-session kill switch),
as opposed to the underlying service functions covered in
test_moderation.py.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Category, Item, LiveSession, User, UserStore

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def make_content_staff(username):
    user = make_user(username, is_staff=True)
    user.groups.add(Group.objects.get(name='إدارة المحتوى'))
    return user


def make_item(owner, **extra):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(owner=owner, title='item', description='d', category=category,
                                condition='used', quantity=1, status='active', **extra)


# ---------------------------------------------------------------------------
# suspend/reactivate users
# ---------------------------------------------------------------------------

def test_suspend_action_without_reason_shows_confirmation_page_not_error():
    target = make_user('sus_target1')
    superuser = User.objects.create_superuser(username='sus_admin1', password='x', email='a@example.com')

    client = Client()
    client.force_login(superuser)
    response = client.post(reverse('admin:core_user_changelist'), {
        'action': 'suspend_users_action',
        '_selected_action': [target.pk],
    })

    assert response.status_code == 200
    assert 'سبب التعليق' in response.content.decode()
    target.refresh_from_db()
    assert target.is_active is True  # nothing happened yet


def test_suspend_action_with_reason_deactivates_target():
    target = make_user('sus_target2')
    superuser = User.objects.create_superuser(username='sus_admin2', password='x', email='a2@example.com')

    client = Client()
    client.force_login(superuser)
    response = client.post(reverse('admin:core_user_changelist'), {
        'action': 'suspend_users_action',
        '_selected_action': [target.pk],
        'apply': '1',
        'reason': 'حساب مزيف',
    })

    assert response.status_code == 302
    target.refresh_from_db()
    assert target.is_active is False
    assert 'حساب مزيف' in target.admin_notes


def test_suspend_action_applied_with_empty_reason_redisplays_form():
    """Distinct from the 'no reason typed yet' case above: this is the
    confirmation form actually submitted (apply=1) with an empty reason —
    must redisplay the form with the error, not silently redirect as if
    nothing happened (a real bug: the action returned None here, which
    Django's action dispatch treats as 'succeeded, redirect back')."""
    target = make_user('sus_target_empty')
    superuser = User.objects.create_superuser(username='sus_admin_empty', password='x', email='ae@example.com')

    client = Client()
    client.force_login(superuser)
    response = client.post(reverse('admin:core_user_changelist'), {
        'action': 'suspend_users_action',
        '_selected_action': [target.pk],
        'apply': '1',
        'reason': '',
    })

    assert response.status_code == 200
    assert 'سبب التعليق' in response.content.decode()
    target.refresh_from_db()
    assert target.is_active is True


def test_suspend_action_skips_the_acting_admin_even_if_self_selected():
    superuser = User.objects.create_superuser(username='sus_self', password='x', email='s@example.com')
    other = make_user('sus_other')

    client = Client()
    client.force_login(superuser)
    client.post(reverse('admin:core_user_changelist'), {
        'action': 'suspend_users_action',
        '_selected_action': [superuser.pk, other.pk],
        'apply': '1',
        'reason': 'bulk sweep',
    })

    superuser.refresh_from_db()
    other.refresh_from_db()
    assert superuser.is_active is True  # never locks itself out
    assert other.is_active is False


def test_reactivate_action_restores_suspended_users():
    target = make_user('sus_target3', is_active=False)
    superuser = User.objects.create_superuser(username='sus_admin3', password='x', email='a3@example.com')

    client = Client()
    client.force_login(superuser)
    client.post(reverse('admin:core_user_changelist'), {
        'action': 'reactivate_users_action',
        '_selected_action': [target.pk],
    })

    target.refresh_from_db()
    assert target.is_active is True


def test_verify_identity_action_sets_flag():
    target = make_user('sus_target4')
    assert target.identity_verified is False
    superuser = User.objects.create_superuser(username='sus_admin4', password='x', email='a4@example.com')

    client = Client()
    client.force_login(superuser)
    client.post(reverse('admin:core_user_changelist'), {
        'action': 'verify_identity_action',
        '_selected_action': [target.pk],
    })

    target.refresh_from_db()
    assert target.identity_verified is True


# ---------------------------------------------------------------------------
# archive items
# ---------------------------------------------------------------------------

def test_archive_action_without_reason_shows_confirmation_page():
    owner = make_user('arc_owner1')
    item = make_item(owner)
    staff = make_content_staff('arc_staff1')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_item_changelist'), {
        'action': 'archive_selected_items',
        '_selected_action': [item.pk],
    })

    assert response.status_code == 200
    assert 'سبب الأرشفة' in response.content.decode()
    item.refresh_from_db()
    assert item.status == 'active'


def test_archive_action_applied_with_empty_reason_redisplays_form():
    owner = make_user('arc_owner_empty')
    item = make_item(owner)
    staff = make_content_staff('arc_staff_empty')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_item_changelist'), {
        'action': 'archive_selected_items',
        '_selected_action': [item.pk],
        'apply': '1',
        'reason': '',
    })

    assert response.status_code == 200
    assert 'سبب الأرشفة' in response.content.decode()
    item.refresh_from_db()
    assert item.status == 'active'


def test_archive_action_with_reason_archives_item():
    owner = make_user('arc_owner2')
    item = make_item(owner)
    staff = make_content_staff('arc_staff2')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_item_changelist'), {
        'action': 'archive_selected_items',
        '_selected_action': [item.pk],
        'apply': '1',
        'reason': 'صورة مخالفة',
    })

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == 'archived'
    assert 'صورة مخالفة' in item.admin_notes


# ---------------------------------------------------------------------------
# store activate/deactivate
# ---------------------------------------------------------------------------

def test_deactivate_and_activate_store_actions():
    owner = make_user('store_owner1')
    store = UserStore.objects.create(user=owner, name='s', is_active=True)
    superuser = User.objects.create_superuser(username='store_admin1', password='x', email='sa@example.com')

    client = Client()
    client.force_login(superuser)
    client.post(reverse('admin:core_userstore_changelist'), {
        'action': 'deactivate_stores_action',
        '_selected_action': [store.pk],
    })
    store.refresh_from_db()
    assert store.is_active is False

    client.post(reverse('admin:core_userstore_changelist'), {
        'action': 'activate_stores_action',
        '_selected_action': [store.pk],
    })
    store.refresh_from_db()
    assert store.is_active is True


# ---------------------------------------------------------------------------
# live session emergency stop
# ---------------------------------------------------------------------------

def test_stop_selected_sessions_action_ends_live_broadcast():
    owner = make_user('live_owner1')
    store = UserStore.objects.create(user=owner, name='s')
    session = LiveSession.objects.create(store=store, title='l', status='live', started_at=timezone.now())
    superuser = User.objects.create_superuser(username='live_admin1', password='x', email='la@example.com')

    client = Client()
    client.force_login(superuser)
    client.post(reverse('admin:core_livesession_changelist'), {
        'action': 'stop_selected_sessions',
        '_selected_action': [session.pk],
    })

    session.refresh_from_db()
    assert session.status == 'ended'


# ---------------------------------------------------------------------------
# low-score review filter
# ---------------------------------------------------------------------------

def test_low_score_filter_surfaces_only_low_rated_reviews():
    from core.models import ProductReview

    owner = make_user('rev_owner')
    reviewer1, reviewer2 = make_user('reviewer_low'), make_user('reviewer_high')
    item = make_item(owner)
    item2 = make_item(owner)
    ProductReview.objects.create(reviewer=reviewer1, item=item, score=1, comment='سيء جدًا')
    ProductReview.objects.create(reviewer=reviewer2, item=item2, score=5, comment='ممتاز')

    superuser = User.objects.create_superuser(username='rev_admin', password='x', email='ra@example.com')
    client = Client()
    client.force_login(superuser)
    response = client.get(reverse('admin:core_productreview_changelist') + '?score_level=low')

    html = response.content.decode()
    assert 'reviewer_low' in html
    assert 'reviewer_high' not in html


# ---------------------------------------------------------------------------
# User change form actually renders with the extended fieldsets
# ---------------------------------------------------------------------------

def test_user_change_form_renders_trust_and_verification_fields():
    target = make_user('fieldset_target')
    superuser = User.objects.create_superuser(username='fieldset_admin', password='x', email='fa@example.com')

    client = Client()
    client.force_login(superuser)
    response = client.get(reverse('admin:core_user_change', args=[target.pk]))

    assert response.status_code == 200
    html = response.content.decode()
    # Editable fields render a real input with this id.
    for field_name in ('identity_verified', 'admin_notes', 'phone_verified'):
        assert f'id_{field_name}' in html, f'{field_name} missing from change form'
    # trust_score/total_sales are readonly — no input id, just a label.
    for label in ('Trust score', 'Total sales'):
        assert label in html, f'"{label}" missing from change form'


def test_user_add_form_still_works_with_extended_fieldsets():
    superuser = User.objects.create_superuser(username='fieldset_admin2', password='x', email='fa2@example.com')
    client = Client()
    client.force_login(superuser)
    response = client.get(reverse('admin:core_user_add'))
    assert response.status_code == 200
