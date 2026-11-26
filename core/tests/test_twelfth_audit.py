"""ما كشفه فحص الخادم الحي: حذفٌ يمحو المبيعات، وعربونٌ بلا من يحصّل الباقي (2026-11-26)."""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core.models import (
    DeliveryCompany, Dispute, Item, ItemTransfer, LiveSession, LiveSessionItem, Report, Reservation, UserStore,
)
from core.tasks import purge_archived_items
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.tests.test_wholesale_money import approve, ask
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def archive_long_ago(*items):
    Item.objects.filter(pk__in=[i.pk for i in items]).update(
        status='archived', updated_at=timezone.now() - timedelta(days=400))


def fresh(w, **kw):
    return Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat,
                               condition='new', quantity=1, status='active', **kw)


def test_a_sold_item_keeps_its_history(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full'); release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller, title='t', description='d', status='resolved')
    archive_long_ago(w.item)
    purge_archived_items()
    assert Item.objects.filter(pk=w.item.pk).exists() and Reservation.objects.filter(pk=r.pk).exists()
    assert Dispute.objects.filter(pk=d.pk).exists()


def test_held_wholesale_money_survives(w):
    t = approve(w, ask(w, 2))
    before = total() + sum(x.held_amount for x in ItemTransfer.objects.all())
    archive_long_ago(w.item, Item.objects.get(pk=t.resulting_item_id))
    purge_archived_items()
    assert total() + sum(x.held_amount for x in ItemTransfer.objects.all()) == before


def test_an_expired_order_or_a_report_is_history_too(w):
    a, b = fresh(w), fresh(w)
    Reservation.objects.create(item=a, buyer=w.buyer, quantity=1, deposit_amount=1, total_price=1,
                               status='expired', expires_at=timezone.now())
    Report.objects.create(reporter=w.buyer, reported_item=b, reason=Report.REASON_CHOICES[0][0])
    archive_long_ago(a, b)
    purge_archived_items()
    assert Item.objects.filter(pk__in=[a.pk, b.pk]).count() == 2


def test_what_nobody_touched_is_still_cleaned(w):
    old, recent = fresh(w), fresh(w)
    archive_long_ago(old)
    Item.objects.filter(pk=recent.pk).update(status='archived')
    purge_archived_items()
    assert not Item.objects.filter(pk=old.pk).exists() and Item.objects.filter(pk=recent.pk).exists()


# ---- a deposit needs a courier

def order(w):
    x = api(w.buyer).post('/api/reservations/create_reservation/',
                          {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id}, format='json')
    return x.data['data']


def pay(w, data, kind):
    return api(w.buyer).post(f"/api/reservations/{data['id']}/complete/", {'payment_type': kind}, format='json')


def test_no_courier_means_no_deposit(w):
    data = order(w)
    assert data['can_pay_deposit'] is False
    x = pay(w, data, 'deposit')
    assert x.status_code == 400 and 'شركة توصيل' in x.data['message'] and bal(w.buyer) == 5000000
    assert pay(w, data, 'full').status_code == 200


def test_with_a_courier_the_deposit_is_back(w):
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    data = order(w)
    assert data['can_pay_deposit'] is True
    assert pay(w, data, 'deposit').status_code == 200


def test_an_inactive_courier_does_not_count(w):
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=False)
    assert pay(w, order(w), 'deposit').status_code == 400


def test_an_auction_win_keeps_its_deposit(w):
    from core.auction_utils import create_winner_reservation
    from core.models import AuctionListing, Bid
    i = fresh(w)
    a = AuctionListing.objects.create(item=i, starting_price=1000, current_price=2000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(days=1), status='ended')
    b = Bid.objects.create(auction=a, bidder=w.buyer, amount=2000, is_winning=True)
    with transaction.atomic():
        r = create_winner_reservation(a, b)
    x = api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'deposit', 'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 200, x.data
