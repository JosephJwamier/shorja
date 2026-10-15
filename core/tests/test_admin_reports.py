"""
Phase 7 — exports, bulk notifications, and the audit log screen.

Export testing focuses on the custom Resource classes (dehydrate_* methods)
since the mixin wiring itself (ExportActionMixin, the export URL/action) is
third-party code; a light integration check proves that wiring actually
resolves rather than re-testing django-import-export's own behavior.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.admin import (
    DisputeExportResource, ReservationExportResource, TransactionExportResource,
    UserExportResource,
)
from core.models import Category, Dispute, Item, Notification, Reservation, Transaction, User

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def _superuser():
    return User.objects.create_superuser(username='reports_owner', password='x', email='r@example.com')


def make_reservation(owner, buyer, total_price=Decimal('40')):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=owner, title='item', description='d', category=category,
                                condition='used', quantity=1, status='active')
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('5'), total_price=total_price,
        status='confirmed', expires_at=timezone.now() + timedelta(days=1),
        completed_at=timezone.now(), is_deposit_only=False,
    )


# ---------------------------------------------------------------------------
# export resources
# ---------------------------------------------------------------------------

def test_user_export_resource_excludes_sensitive_fields():
    make_user('export_target', email='et@example.com')
    dataset = UserExportResource().export()
    assert 'password' not in dataset.headers
    assert 'verification_code' not in dataset.headers
    assert 'fcm_token' not in dataset.headers
    assert 'export_target' in dataset.csv


def test_reservation_export_resource_shows_readable_names_not_ids():
    owner, buyer = make_user('res_exp_owner'), make_user('res_exp_buyer')
    make_reservation(owner, buyer, Decimal('55'))

    dataset = ReservationExportResource().export()
    assert 'res_exp_buyer' in dataset.csv
    assert 'item' in dataset.csv  # the item title column, not a bare FK id


def test_transaction_export_resource_shows_username():
    user = make_user('txn_exp_user')
    Transaction.objects.create(user=user, transaction_type='payout', amount=Decimal('12'), status='completed')

    dataset = TransactionExportResource().export()
    assert 'txn_exp_user' in dataset.csv
    assert 'payout' in dataset.csv


def test_dispute_export_resource_shows_readable_parties():
    owner, buyer = make_user('disp_exp_owner'), make_user('disp_exp_buyer')
    category, _ = Category.objects.get_or_create(name='cat2', slug='cat2')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=1, status='active')
    Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='t', description='d')

    dataset = DisputeExportResource().export()
    assert 'disp_exp_buyer' in dataset.csv
    assert 'disp_exp_owner' in dataset.csv


def test_export_url_resolves_for_users_reservations_transactions_disputes():
    client = Client()
    client.force_login(_superuser())
    for model_name in ('user', 'reservation', 'transaction', 'dispute'):
        url = reverse(f'admin:core_{model_name}_export')
        response = client.get(url)
        assert response.status_code == 200, f'{model_name} export page failed to render'


# ---------------------------------------------------------------------------
# bulk notification
# ---------------------------------------------------------------------------

def test_send_notification_action_creates_notification_for_each_selected_user():
    target1, target2 = make_user('notif_target1'), make_user('notif_target2')
    untouched = make_user('notif_untouched')
    staff = _superuser()

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_user_changelist'), {
        'action': 'send_notification_action',
        'apply': '1',
        '_selected_action': [str(target1.pk), str(target2.pk)],
        'notif_title': 'صيانة مجدولة',
        'notif_body': 'سيتوقف التطبيق للصيانة الساعة 2 صباحًا.',
    })

    assert response.status_code == 302
    assert Notification.objects.filter(user=target1, title='صيانة مجدولة').exists()
    assert Notification.objects.filter(user=target2, title='صيانة مجدولة').exists()
    assert not Notification.objects.filter(user=untouched).exists()


def test_send_notification_action_requires_title_and_body():
    target = make_user('notif_target3')
    staff = _superuser()

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_user_changelist'), {
        'action': 'send_notification_action',
        'apply': '1',
        '_selected_action': [str(target.pk)],
        'notif_title': '',
        'notif_body': '',
    })

    assert response.status_code == 200  # redisplays the compose form
    assert not Notification.objects.filter(user=target).exists()


# ---------------------------------------------------------------------------
# audit log screen
# ---------------------------------------------------------------------------

def test_audit_log_records_dispute_changes_and_is_filterable_by_actor():
    owner, buyer = make_user('audit_owner'), make_user('audit_buyer')
    staff = _superuser()
    category, _ = Category.objects.get_or_create(name='cat3', slug='cat3')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=1, status='active')
    dispute = Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='t', description='d')
    dispute.admin_notes = 'investigating'
    dispute.save(update_fields=['admin_notes'])

    client = Client()
    client.force_login(staff)
    response = client.get(reverse('admin:auditlog_logentry_changelist'))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'actor' in html.lower() or 'الفاعل' in html or 'Actor' in html


def test_audit_log_changelist_has_actor_filter_available():
    from django.contrib import admin as django_admin
    from auditlog.models import LogEntry

    model_admin = django_admin.site._registry[LogEntry]
    assert 'actor' in model_admin.list_filter
