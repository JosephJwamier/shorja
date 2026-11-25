"""ما كشفه الفحص العاشر: السرعة مع الكبر، وعنوان الاستلام الفارغ (2026-11-26)."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    DeliveryCompany, DeliveryOrder, FixedPriceListing, Item, ItemVariant, Location, Reservation,
    StoreFollow, Transaction, User, UserStore,
)
from core.services.withdrawals import earnings_still_disputable
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def stock_up(w, n):
    Item.objects.bulk_create([Item(owner=w.seller, current_holder=w.seller, title='t%d' % i, description='d', category=w.cat,
                                   condition='new', quantity=5, status='active') for i in range(n)])
    new = list(Item.objects.filter(variants__isnull=True))
    ItemVariant.objects.bulk_create([ItemVariant(item=i, quantity=5, attributes={}) for i in new])
    FixedPriceListing.objects.bulk_create([FixedPriceListing(item=i, price=1000, deposit_percentage=10, deposit_amount=100) for i in new])


def test_a_big_shop_is_served_in_pages(w):
    UserStore.objects.create(user=w.seller, name='s', is_active=True)
    stock_up(w, 250)
    url = f'/api/stores/public/{w.seller.username}/items/'
    with CaptureQueriesContext(connection) as q:
        first = APIClient().get(url).json()
    assert len(first['data']) == 100 and first['count'] == 251 and first['next_page'] == 2
    assert len(q) < 130, len(q)
    seen = [i['id'] for i in first['data']]
    page = first['next_page']
    while page:
        d = APIClient().get(url, {'page': page}).json()
        seen += [i['id'] for i in d['data']]; page = d['next_page']
    assert len(seen) == len(set(seen)) == 251
    assert len(APIClient().get(url, {'page_size': 99999}).json()['data']) == 100
    assert APIClient().get(url, {'page': 'abc'}).status_code == 200


def test_stores_come_in_pages(w):
    us = [user('st%d' % i) for i in range(60)]
    UserStore.objects.bulk_create([UserStore(user=u, name='n%d' % i, is_active=True) for i, u in enumerate(us)])
    a = APIClient().get('/api/stores/').json()
    b = APIClient().get('/api/stores/?page=2').json()
    assert (len(a['data']), len(b['data']), a['count'], b['next_page']) == (50, 10, 60, None)
    assert not {s['id'] for s in a['data']} & {s['id'] for s in b['data']}
    assert len(APIClient().get('/api/stores/?search=n59').json()['data']) == 1


def test_following_does_not_grow_per_item(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    StoreFollow.objects.create(follower=w.buyer, store=s)
    stock_up(w, 12)
    for url in ('/api/stores/following/', '/api/stores/following-items/'):
        with CaptureQueriesContext(connection) as q:
            assert api(w.buyer).get(url).status_code == 200
        assert len(q) < 70, (url, len(q))


# ---- the disputable-earnings sum says what Reservation.dispute_deadline says

def sale(w, **kw):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full'); release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(**{'status': 'confirmed', **kw})
    return Reservation.objects.get(pk=r.pk)


@pytest.mark.parametrize('days,warranty', [(1, 1), (9, 7), (10, 7), (11, 7), (40, 30), (34, 30), (200, 365), (364, 365)])
@pytest.mark.parametrize('anchor', ['released', 'approved', 'delivered'])
def test_the_sum_agrees_with_the_order(w, days, warranty, anchor):
    then = timezone.now() - timedelta(days=days)
    kw = {'warranty_days': warranty, 'payment_released_at': timezone.now() - timedelta(days=1)}
    if anchor == 'released':
        kw['payment_released_at'] = then
    if anchor == 'approved':
        kw['buyer_approved_at'] = then
    r = sale(w, **kw)
    if anchor == 'delivered':
        co = DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
        DeliveryOrder.objects.create(reservation=r, company=co, direction='outbound', status='confirmed', delivered_at=then)
    r = Reservation.objects.get(pk=r.pk)
    open_ = timezone.now() <= r.dispute_deadline
    assert (earnings_still_disputable(w.seller) > 0) == open_


def test_a_disputed_sale_is_always_waiting(w):
    sale(w, status='disputed', payment_released_at=timezone.now() - timedelta(days=300))
    assert earnings_still_disputable(w.seller) > 0


def test_the_wallet_does_not_grow_with_sales(w):
    def count():
        with CaptureQueriesContext(connection) as q:
            api(w.seller).get('/api/wallet/me/')
        return len(q)
    sale(w); a = count()
    sale(w); sale(w); sale(w)
    assert count() <= a


# ---- pickup address

def company():
    return DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)


def paid(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    return r


def test_an_order_needs_somewhere_to_collect_it(w):
    company()
    r = paid(w)
    url = f'/api/reservations/{r.id}/send-to-delivery/'
    x = api(w.seller).post(url)
    assert x.status_code == 400 and x.data['code'] == 'no_pickup_address' and not DeliveryOrder.objects.exists()
    l = Location.objects.create(user=w.seller, name='x', address_line1='a', city='b', state='baghdad', contact_person='p', contact_phone='07701234567')
    assert api(User.objects.get(pk=w.seller.pk)).post(url).status_code == 201
    assert DeliveryOrder.objects.get().from_address
    r2 = paid(w)
    l.is_active = False; l.save()
    assert api(User.objects.get(pk=w.seller.pk)).post(f'/api/reservations/{r2.id}/send-to-delivery/').status_code == 400


def test_the_portal_asks_for_the_last_days_only(w):
    c = company()
    for age in (1, 60):
        o = DeliveryOrder.objects.create(reservation=paid(w), company=c, direction='outbound', status='confirmed')
        DeliveryOrder.objects.filter(pk=o.pk).update(assigned_at=timezone.now() - timedelta(days=age))
    n = lambda q: api(c.account).get('/api/delivery-orders/' + q).json()['count']
    assert (n(''), n('?days=30'), n('?days=abc'), n('?days=99999999')) == (2, 1, 2, 2)
