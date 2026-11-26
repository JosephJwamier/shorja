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
