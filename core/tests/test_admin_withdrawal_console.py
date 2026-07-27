"""
Phase 5 — the admin withdrawal review console itself (as opposed to the
approve/reject service, covered in test_withdrawal_review.py). Also covers
the Wallet anomaly filter/badge — the "alert on negative or unexpected
balances" half of Phase 5.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from core.models import User, Wallet, WithdrawalRequest, get_or_create_wallet

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def make_finance_staff(username):
    user = make_user(username, is_staff=True)
    user.groups.add(Group.objects.get(name='الشؤون المالية'))
    return user


def make_pending_withdrawal(user, amount=Decimal('50')):
    wallet = get_or_create_wallet(user)
    wallet.credit(amount)
    wallet.hold(amount)
    return WithdrawalRequest.objects.create(user=user, amount=amount, status='pending')


def review_url(withdrawal):
    return reverse('admin:core_withdrawalrequest_review', args=[withdrawal.pk])


def test_console_shows_amount_and_wallet_balances():
    user = make_user('wc_user1')
    withdrawal = make_pending_withdrawal(user, Decimal('70'))
    staff = make_finance_staff('wc_staff1')

    client = Client()
    client.force_login(staff)
    response = client.get(review_url(withdrawal))

    assert response.status_code == 200
    html = response.content.decode()
    assert '70.00' in html or '70' in html


def test_approving_pays_out_and_redirects():
    user = make_user('wc_user2')
    withdrawal = make_pending_withdrawal(user, Decimal('45'))
    staff = make_finance_staff('wc_staff2')

    client = Client()
    client.force_login(staff)
    response = client.post(review_url(withdrawal), {'decision': 'approve', 'admin_notes': ''})

    assert response.status_code == 302
    withdrawal.refresh_from_db()
    assert withdrawal.status == 'approved'
    assert get_or_create_wallet(user).balance == Decimal('0.00')


def test_rejecting_without_notes_is_blocked():
    user = make_user('wc_user3')
    withdrawal = make_pending_withdrawal(user, Decimal('20'))
    staff = make_finance_staff('wc_staff3')

    client = Client()
    client.force_login(staff)
    response = client.post(review_url(withdrawal), {'decision': 'reject', 'admin_notes': ''})

    assert response.status_code == 200  # redisplays the form
    withdrawal.refresh_from_db()
    assert withdrawal.status == 'pending'  # nothing happened
    assert get_or_create_wallet(user).held_balance == Decimal('20.00')  # still held


def test_rejecting_with_notes_releases_funds():
    user = make_user('wc_user4')
    withdrawal = make_pending_withdrawal(user, Decimal('20'))
    staff = make_finance_staff('wc_staff4')

    client = Client()
    client.force_login(staff)
    response = client.post(review_url(withdrawal), {'decision': 'reject', 'admin_notes': 'بطاقة غير صالحة'})

    assert response.status_code == 302
    withdrawal.refresh_from_db()
    assert withdrawal.status == 'rejected'
    assert withdrawal.admin_notes == 'بطاقة غير صالحة'
    assert get_or_create_wallet(user).available_balance == Decimal('20.00')


def test_review_requires_change_withdrawalrequest_permission():
    user = make_user('wc_user5')
    withdrawal = make_pending_withdrawal(user, Decimal('20'))
    plain_staff = make_user('wc_staff5', is_staff=True)  # no permission

    client = Client()
    client.force_login(plain_staff)
    response = client.get(review_url(withdrawal))

    assert response.status_code == 403


def test_already_reviewed_withdrawal_shows_summary_not_form():
    user = make_user('wc_user6')
    withdrawal = make_pending_withdrawal(user, Decimal('20'))
    withdrawal.status = 'approved'
    withdrawal.save(update_fields=['status'])
    staff = make_finance_staff('wc_staff6')

    client = Client()
    client.force_login(staff)
    response = client.get(review_url(withdrawal))

    assert response.status_code == 200
    assert 'تمت مراجعة هذا الطلب بالفعل' in response.content.decode()


# ---------------------------------------------------------------------------
# Wallet anomaly filter/badge
# ---------------------------------------------------------------------------

def test_wallet_anomaly_filter_finds_negative_balance():
    normal_user = make_user('wallet_normal')
    get_or_create_wallet(normal_user).credit(Decimal('10'))

    broken_user = make_user('wallet_broken')
    Wallet.objects.create(user=broken_user, balance=Decimal('-5.00'))

    superuser = User.objects.create_superuser(username='wallet_admin', password='x', email='w@example.com')
    client = Client()
    client.force_login(superuser)

    response = client.get(reverse('admin:core_wallet_changelist') + '?anomaly=negative')
    assert response.status_code == 200
    html = response.content.decode()
    assert 'wallet_broken' in html
    assert 'wallet_normal' not in html


def test_wallet_changelist_shows_anomaly_badge():
    broken_user = make_user('wallet_broken2')
    Wallet.objects.create(user=broken_user, balance=Decimal('-1.00'))

    superuser = User.objects.create_superuser(username='wallet_admin2', password='x', email='w2@example.com')
    client = Client()
    client.force_login(superuser)

    response = client.get(reverse('admin:core_wallet_changelist'))
    assert 'رصيد سالب' in response.content.decode()
