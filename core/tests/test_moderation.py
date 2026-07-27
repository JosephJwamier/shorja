"""
Phase 6 — core/services/moderation.py regression tests.
"""
from decimal import Decimal

import pytest
from django.utils import timezone

from core.models import Category, Item, LiveSession, LiveSessionItem, User, UserStore
from core.services.moderation import (
    archive_item, reactivate_user, stop_live_session, suspend_user,
)

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def make_item(owner):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(owner=owner, title='item', description='d', category=category,
                                condition='good', quantity=1, status='active')


# ---------------------------------------------------------------------------
# suspend / reactivate
# ---------------------------------------------------------------------------

def test_suspend_user_requires_a_reason():
    user = make_user('mod_u1')
    admin = make_user('mod_admin1')
    with pytest.raises(ValueError):
        suspend_user(user, reason='', actor=admin)
    with pytest.raises(ValueError):
        suspend_user(user, reason='   ', actor=admin)
    user.refresh_from_db()
    assert user.is_active is True  # nothing happened


def test_suspend_user_deactivates_and_logs_reason():
    user = make_user('mod_u2')
    admin = make_user('mod_admin2')

    suspend_user(user, reason='إساءة للمشترين', actor=admin)

    user.refresh_from_db()
    assert user.is_active is False
    assert 'إساءة للمشترين' in user.admin_notes
    assert 'mod_admin2' in user.admin_notes
    assert 'تعليق الحساب' in user.admin_notes


def test_reactivate_user_restores_access_and_appends_note():
    user = make_user('mod_u3', is_active=False)
    admin = make_user('mod_admin3')

    reactivate_user(user, actor=admin)

    user.refresh_from_db()
    assert user.is_active is True
    assert 'إعادة تفعيل الحساب' in user.admin_notes


def test_suspend_then_reactivate_preserves_note_history():
    user = make_user('mod_u4')
    admin = make_user('mod_admin4')

    suspend_user(user, reason='سبب أول', actor=admin)
    reactivate_user(user, actor=admin)

    user.refresh_from_db()
    assert 'سبب أول' in user.admin_notes
    assert 'إعادة تفعيل الحساب' in user.admin_notes
    assert user.admin_notes.count('mod_admin4') == 2


# ---------------------------------------------------------------------------
# archive_item
# ---------------------------------------------------------------------------

def test_archive_item_requires_a_reason():
    owner = make_user('mod_owner1')
    item = make_item(owner)
    admin = make_user('mod_admin5')

    with pytest.raises(ValueError):
        archive_item(item, reason='', actor=admin)
    item.refresh_from_db()
    assert item.status == 'active'


def test_archive_item_sets_archived_status_and_logs_reason():
    owner = make_user('mod_owner2')
    item = make_item(owner)
    admin = make_user('mod_admin6')

    archive_item(item, reason='محتوى مخالف', actor=admin)

    item.refresh_from_db()
    assert item.status == 'archived'
    assert 'محتوى مخالف' in item.admin_notes
    assert 'أرشفة المنتج' in item.admin_notes


# ---------------------------------------------------------------------------
# stop_live_session
# ---------------------------------------------------------------------------

def test_stop_live_session_ends_broadcast_and_resets_viewers():
    owner = make_user('mod_owner3')
    store = UserStore.objects.create(user=owner, name='s')
    session = LiveSession.objects.create(
        store=store, title='live', status='live', current_viewer_count=42, started_at=timezone.now(),
    )
    item = make_item(owner)
    queued = LiveSessionItem.objects.create(
        session=session, item=item, price=Decimal('10'), deposit_amount=Decimal('1'), status='queued',
    )

    stop_live_session(session)

    session.refresh_from_db()
    queued.refresh_from_db()
    assert session.status == 'ended'
    assert session.current_viewer_count == 0
    assert session.ended_at is not None
    assert queued.status == 'skipped'
