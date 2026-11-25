"""ما كشفه الفحص الحادي عشر: رابط المندوب، العمولة، السحب، والنزاعات (2026-11-26)."""
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core.models import (
    CouponCollection, DeliveryCompany, DeliveryOrder, Dispute, DisputeImage, InstallmentPlan, Item, ItemVariant,
    Location, Notification, PaymentIntent, Representative, Reservation, StoreCoupon, Transaction, User,
    UserStore, WithdrawalRequest, get_or_create_wallet,
)
from core.services.commission import platform_account
from core.services.withdrawals import approve_withdrawal, reject_withdrawal
from core.tasks import process_expired_reservations, recheck_settled_payments
from core.tests.test_fourth_audit import small
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def stock(w):
    return Item.objects.get(pk=w.item.pk).quantity


# ---- a representative's order is an ordinary order

def through_rep(w, n=2):
    a, _ = Representative.objects.get_or_create(item=w.item, owner=w.seller, representative=w.rep,
                                                defaults={'commission_percentage': 5, 'status': 'accepted'})
    x = api(w.buyer).post(f'/api/representatives/{a.id}/create_reservation_for_buyer/',
                          {'item_id': w.item.id, 'quantity': n, 'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 201, x.data
    return Reservation.objects.get(pk=x.data['data']['id'])


def pay(w, r, kind='full', who=None):
    return api(who or w.buyer).post(f'/api/reservations/{r.id}/complete/',
                                    {'payment_type': kind, 'delivery_location_id': w.loc.id}, format='json')


def test_reserving_takes_nothing_and_paying_does(w):
    r = through_rep(w, 4)
    assert stock(w) == 10 and r.expires_at > timezone.now() + timedelta(hours=23)
    assert pay(w, r).status_code == 200
    assert stock(w) == 6


def test_a_whole_shelf_may_be_reserved_and_stays_on_sale(w):
    through_rep(w, 10)
    assert stock(w) == 10 and Item.objects.get(pk=w.item.pk).status == 'active'


def test_expiry_gives_nothing_back_and_ends_the_commission(w):
    r = through_rep(w, 4)
    Reservation.objects.update(expires_at=timezone.now() - timedelta(hours=1))
    process_expired_reservations()
    r.refresh_from_db()
    assert (stock(w), r.status) == (10, 'expired')
    assert Transaction.objects.get(transaction_type='commission').status == 'cancelled'


def test_cancelling_before_paying_adds_no_stock(w):
    r = through_rep(w, 4)
    assert api(w.seller).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert stock(w) == 10


def test_cancelling_after_paying_returns_it(w):
    r = through_rep(w, 4)
    pay(w, r)
    api(w.seller).post(f'/api/reservations/{r.id}/cancel/')
    assert stock(w) == 10


def test_an_order_from_before_keeps_its_held_stock(w):
    r = through_rep(w, 4)
    Reservation.objects.filter(pk=r.pk).update(stock_held=True)
    ItemVariant.objects.get(item=w.item).take_stock(4)
    assert pay(w, r).status_code == 200 and stock(w) == 6
    old = through_rep(w, 2)
    Reservation.objects.filter(pk=old.pk).update(stock_held=True, expires_at=timezone.now() - timedelta(hours=1))
    ItemVariant.objects.get(item=w.item).take_stock(2)
    process_expired_reservations()
    assert stock(w) == 6


def test_an_installment_request_parks_nothing(w):
    r = through_rep(w, 10)
    InstallmentPlan.objects.create(
        reservation=r, buyer=w.buyer, provider='qicard', reference='X', state='requested', principal=1, months=10,
        customer_fee_percent=0, customer_fee_amount=0, total_payable=1, monthly_amount=1, merchant_fee_percent=0,
        merchant_fee_amount=0, platform_settlement=1, seller_fee_amount=0, expires_at=timezone.now() + timedelta(days=3))
    Reservation.objects.update(expires_at=timezone.now() - timedelta(hours=20))
    process_expired_reservations()
    assert stock(w) == 10


@pytest.mark.parametrize('rep', [True, False])
def test_too_late_says_so(w, rep):
    def order(n):
        if rep:
            return through_rep(w, n)
        x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': n, 'delivery_location_id': w.loc.id}, format='json')
        return Reservation.objects.get(pk=x.data['data']['id'])
    r = order(5)
    ItemVariant.objects.get(item=w.item).take_stock(8)
    x = pay(w, r)
    assert x.status_code == 400 and x.data['code'] == 'out_of_stock' and 'لم يبقَ سوى 2' in x.data['message']
    ItemVariant.objects.get(item=w.item).take_stock(2)
    x = pay(w, r)
    assert x.data['code'] == 'out_of_stock' and 'نفدت الكمية لأن إكمال الحجز تأخر' in x.data['message']
    assert bal(w.buyer) == 5000000


# ---- commission

def test_a_deposit_is_confirmed_only_after_delivery(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'deposit')
    url = f'/api/reservations/{r.id}/approve_and_release_payment/'
    x = api(w.buyer).post(url)
    assert x.status_code == 400 and x.data['code'] == 'needs_delivery'


def test_a_paid_order_may_be_handed_over_in_person(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    assert api(w.buyer).post(f'/api/reservations/{r.id}/approve_and_release_payment/').status_code == 200


def test_a_coupon_is_never_the_whole_price(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    cc = CouponCollection.objects.create(coupon=StoreCoupon.objects.create(store=s, code='A', amount=P - 1), user=w.buyer)
    api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'coupon_collection_id': cc.id}, format='json')
    r = Reservation.objects.get()
    assert (r.total_price, r.coupon_discount) == (P * 3 / 10, P * 7 / 10)


# ---- withdrawals

CARD = {'account_number': '6330123456789012', 'holder_name': 'x'}


def withdraw(w, amount=5000):
    User.objects.filter(pk=w.buyer.pk).update(phone='+9647801234567', phone_verified=True)
    return api(User.objects.get(pk=w.buyer.pk)).post('/api/wallet/withdraw/', {'amount': str(amount), **CARD}, format='json')


def test_withdrawal_bounds(w):
    assert withdraw(w, 4999).status_code == 400
    assert [withdraw(w).status_code for _ in range(3)] == [201, 201, 400]


def test_cancelling_frees_the_money(w):
    wd = withdraw(w).data['data']['id']
    assert api(w.buyer2).post(f'/api/wallet/withdrawals/{wd}/cancel/').status_code == 404
    assert api(w.buyer).post(f'/api/wallet/withdrawals/{wd}/cancel/').status_code == 200
    row = WithdrawalRequest.objects.get()
    wallet = get_or_create_wallet(w.buyer); wallet.refresh_from_db()
    assert (row.status, row.account_number, wallet.held_balance, wallet.balance) == ('cancelled', '', 0, 5000000)
    assert api(w.buyer).post(f'/api/wallet/withdrawals/{wd}/cancel/').status_code == 400
    with pytest.raises(Exception):
        approve_withdrawal(row, reviewed_by=w.rep)
    assert withdraw(w).status_code == 201
    assert api(w.buyer).get('/api/wallet/activity/').status_code == 200


def test_both_sides_are_told(w, django_capture_on_commit_callbacks):
    boss = User.objects.create_superuser(username='boss', password='x')
    with django_capture_on_commit_callbacks(execute=True):
        withdraw(w)
    assert Notification.objects.filter(user=boss, title='طلب سحب جديد').exists()
    with django_capture_on_commit_callbacks(execute=True):
        reject_withdrawal(WithdrawalRequest.objects.get(), reviewed_by=boss, admin_notes='رقم {خاطئ}')
    assert 'رقم' in Notification.objects.get(user=w.buyer, title='رُفض طلب السحب').body
    withdraw(w)
    with django_capture_on_commit_callbacks(execute=True):
        approve_withdrawal(WithdrawalRequest.objects.get(status='pending'), reviewed_by=boss)
    assert '9012' in Notification.objects.get(user=w.buyer, title='حُوّل مبلغ السحب').body


# ---- card payments taken back

def test_a_payment_taken_back_is_flagged_once_and_told(w):
    from core.services.payments.base import PaymentStatus
    User.objects.create_superuser(username='boss', password='x')
    old = timezone.now() - timedelta(days=20)
    for ref in ('back', 'fine'):
        PaymentIntent.objects.create(user=w.buyer, provider='alqaseh', amount=50000, reference=ref, state='succeeded', provider_payment_id=ref, applied_at=old)
    PaymentIntent.objects.update(updated_at=old)
    answer = lambda self, intent: PaymentStatus(state='cancelled' if intent.reference == 'back' else 'succeeded', raw={})
    with patch('core.services.payments.alqaseh.AlqasehProvider.fetch_status', answer):
        assert '1 payment' in recheck_settled_payments()
        assert '0 payment' in recheck_settled_payments()
    assert PaymentIntent.objects.get(reference='back').reversed_at and not PaymentIntent.objects.get(reference='fine').reversed_at
    assert bal(w.buyer) == 5000000 and Notification.objects.filter(user__username='boss').count() == 1


# ---- disputes

def dispute(w):
    return Dispute.objects.create(item=w.item, complainant=w.buyer, defendant=w.seller, title='t', description='d')


def reply(u, d, text='x', images=()):
    return api(u).post(f'/api/disputes/{d.id}/reply/', {'text': text, 'images': list(images)}, format='multipart')


def test_both_sides_speak_and_show(w, tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    d = dispute(w)
    x = reply(w.buyer, d, '', [small('PNG', 'a.png')])
    assert x.status_code == 201, x.data
    x = reply(w.seller, d, 'لم يصل بهذا الشكل')
    assert [(r['is_complainant'], len(r['images'])) for r in x.data['data']['replies']] == [(True, 1), (False, 0)]
    assert x.data['data']['can_reply'] and not DisputeImage.objects.get().image.name.endswith('/a.png')
    assert reply(w.buyer2, d).status_code == 404
    assert api(w.seller).get('/api/disputes/').json()['results'][0]['replies'][1]['text'] == 'لم يصل بهذا الشكل'


def test_reply_bounds(w, tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    d = dispute(w)
    assert reply(w.buyer, d, '').status_code == 400
    assert reply(w.buyer, d, 'x' * 3001).status_code == 400
    assert reply(w.buyer, d, 'x', [small('BMP', 'a.png')]).status_code == 400
    assert reply(w.buyer, d, 'x', [small('PNG', f'{n}.png') for n in range(6)]).status_code == 400
    assert not d.replies.exists()
    assert [reply(w.buyer, d).status_code for _ in range(11)].count(201) == 10
    Dispute.objects.update(status='resolved')
    x = reply(w.seller, d)
    assert x.status_code == 400 and 'حُسم' in x.data['message']


def test_the_other_side_is_told(w, django_capture_on_commit_callbacks):
    d = dispute(w)
    with django_capture_on_commit_callbacks(execute=True):
        reply(w.buyer, d)
    assert Notification.objects.filter(user=w.seller, title='رد جديد على النزاع').exists()


def test_the_admin_reads_them(w, client):
    from django.urls import reverse
    d = dispute(w); reply(w.seller, d, 'كلام البائع')
    boss = User.objects.create_superuser(username='boss', password='x')
    client.force_login(boss, backend='django.contrib.auth.backends.ModelBackend')
    html = client.get(reverse('admin:core_dispute_resolve', args=[d.pk])).content.decode()
    assert 'كلام البائع' in html
