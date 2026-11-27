"""تسليم الطلب يداً بيد: بلا شركة توصيل كان المال ينتظر ضغطة المشتري للأبد،
والمشتري الذي استلم في أول ساعتين يستطيع الإلغاء واسترداد المبلغ (2026-11-26)."""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core.models import Dispute, Reservation
from core.tasks import release_payments_after_week, process_ended_auctions
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def paid(w, kind='full'):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, kind)
    return r


def hand_over(w, r, who=None):
    return api(who or w.seller).post(f'/api/reservations/{r.id}/hand-over/')


def test_the_seller_records_it_and_the_buyer_cannot_cancel_alone(w):
    r = paid(w)
    assert api(w.seller).get(f'/api/reservations/{r.id}/').data['can_hand_over'] is True
    x = hand_over(w, r)
    assert x.status_code == 200 and x.data['data']['handed_over_at']
    assert x.data['data']['can_hand_over'] is False
    c = api(w.buyer).post(f'/api/reservations/{r.id}/cancel/')
    assert c.status_code == 400 and Reservation.objects.get(pk=r.pk).status == 'completed'


def test_only_the_seller_and_only_once(w):
    r = paid(w)
    assert hand_over(w, r, w.buyer).status_code == 403
    assert hand_over(w, r).status_code == 200
    assert hand_over(w, r).status_code == 400


def test_a_deposit_still_needs_the_courier(w):
    r = paid(w, 'deposit')
    assert hand_over(w, r).status_code == 400


def test_the_money_goes_to_the_seller_after_the_warranty(w):
    r = paid(w)
    hand_over(w, r)
    seller_before = bal(w.seller)
    release_payments_after_week()
    assert not Reservation.objects.get(pk=r.pk).payment_released_to_seller
    Reservation.objects.filter(pk=r.pk).update(handed_over_at=timezone.now() - timedelta(days=r.warranty_days + 1))
    release_payments_after_week()
    r.refresh_from_db()
    assert r.payment_released_to_seller and r.status == 'confirmed' and bal(w.seller) > seller_before


def test_an_open_dispute_holds_the_money(w):
    r = paid(w)
    hand_over(w, r)
    Reservation.objects.filter(pk=r.pk).update(handed_over_at=timezone.now() - timedelta(days=60))
    Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                           title='t', description='d', status='open')
    release_payments_after_week()
    assert not Reservation.objects.get(pk=r.pk).payment_released_to_seller


def test_not_handed_over_is_never_released_alone(w):
    r = paid(w)
    Reservation.objects.filter(pk=r.pk).update(completed_at=timezone.now() - timedelta(days=60))
    release_payments_after_week()
    assert not Reservation.objects.get(pk=r.pk).payment_released_to_seller


def test_the_seller_agreeing_still_opens_the_cancel(w):
    r = paid(w)
    hand_over(w, r)
    api(w.seller).post(f'/api/reservations/{r.id}/approve_cancellation/')
    assert api(w.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200


def test_the_auction_sweep_survives_a_failure(w, monkeypatch):
    from core.models import AuctionListing
    import core.auction_utils as au
    from core.models import Item
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat,
                            condition='new', quantity=1, status='active')
    AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                  start_time=timezone.now() - timedelta(days=2),
                                  end_time=timezone.now() - timedelta(days=1), status='active')

    def boom(*a, **k):
        raise RuntimeError('x')
    monkeypatch.setattr(au, 'settle_on_winner', boom)
    assert 'Processed 0' in process_ended_auctions()


# ---- a handed-over order that comes back: the piece is with the buyer

def stock(w):
    w.item.default_variant.refresh_from_db()
    return w.item.default_variant.quantity


def handed(w):
    r = paid(w)
    assert hand_over(w, r).status_code == 200
    return r


def refund_by_dispute(w, r, action='refund_buyer', **kw):
    from core.services.disputes import resolve_dispute
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', status='open')
    Reservation.objects.filter(pk=r.pk).update(status='disputed')
    resolve_dispute(d, resolution_action=action, **kw)


def test_refunding_a_handed_over_order_keeps_the_stock_until_the_seller_has_it(w):
    before = stock(w)
    r = handed(w)
    refund_by_dispute(w, r)
    assert stock(w) == before - 1  # the piece is still with the buyer
    seen = api(w.seller).get(f'/api/reservations/{r.id}/').data
    assert seen['can_mark_goods_returned'] is True
    assert api(w.buyer).get(f'/api/reservations/{r.id}/').data['can_mark_goods_returned'] is False

    back = api(w.seller).post(f'/api/reservations/{r.id}/goods-returned/')
    assert back.status_code == 200, back.data
    assert stock(w) == before
    again = api(w.seller).post(f'/api/reservations/{r.id}/goods-returned/')
    assert again.status_code == 400 and stock(w) == before


def test_the_seller_cancelling_after_hand_over_waits_for_the_piece_too(w):
    before = stock(w)
    r = handed(w)
    assert api(w.seller).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert stock(w) == before - 1
    assert api(w.seller).post(f'/api/reservations/{r.id}/goods-returned/').status_code == 200
    assert stock(w) == before


def test_an_order_never_handed_over_gets_its_stock_back_at_once(w):
    before = stock(w)
    r = paid(w)
    assert api(w.seller).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert stock(w) == before
    assert api(w.seller).post(f'/api/reservations/{r.id}/goods-returned/').status_code == 400


def test_only_the_seller_puts_it_back(w):
    r = handed(w)
    refund_by_dispute(w, r)
    assert api(w.buyer).post(f'/api/reservations/{r.id}/goods-returned/').status_code in (403, 404)


# ---- a partial refund shows in the seller's book

def test_a_partial_refund_lowers_the_sale_in_the_book(w):
    from decimal import Decimal
    from core.models import UserStore
    UserStore.objects.create(user=w.seller, name='s', is_active=True)
    r = handed(w)
    refund_by_dispute(w, r, action='partial_refund', partial_refund_amount=Decimal('20000'))
    r.refresh_from_db()
    entry = r.ledger_entry
    assert entry.amount == P - Decimal('20000') and not entry.is_void


# ---- a prepaid delivery fee for a trip that never happened goes back

def test_handing_over_returns_a_prepaid_delivery_fee(w):
    from decimal import Decimal
    from unittest.mock import patch
    from core.models import DeliveryCompany
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True,
                                   intra_governorate_fee=5000, inter_governorate_fee=5000)
    r = paid(w)
    r.refresh_from_db()
    fee = r.delivery_fee
    assert fee > 0
    before = bal(w.buyer)
    with patch('core.views.notify_push') as push:
        assert hand_over(w, r).status_code == 200
    r.refresh_from_db()
    assert r.delivery_fee == Decimal('0.00')
    assert bal(w.buyer) == before + fee
    assert push.call_args.args[0] == w.buyer  # a push, which the app does receive


# ---- one reminder, and only for an order that can still be completed

def test_no_reminder_for_an_order_that_can_no_longer_be_completed(w):
    from datetime import timedelta as td
    from unittest.mock import patch
    from core.models import Item
    from core.tasks import send_pending_reservation_reminders
    soon = timezone.now() + td(hours=4)
    ok = res(w, expires_at=soon)
    far = res(w, buyer=user('b4', 1), expires_at=timezone.now() + td(hours=20))
    other = Item.objects.create(owner=w.seller, current_holder=w.seller, title='x', description='d',
                                category=w.cat, condition='new', quantity=1, status='archived')
    gone = res(w, buyer=user('b5', 1), item=other, expires_at=soon)
    with patch('core.notifications.notify_reservation_expiring') as remind:
        send_pending_reservation_reminders()
    assert [c.args[0].pk for c in remind.call_args_list] == [ok.pk]
    assert far.pk and gone.pk
