"""أعطال حركة المال التي كشفها فحص 2026-11-24، كلّ اختبار يثبّت السلوك الصحيح.

أداة الكشف في معظمها واحدة: مجموع المحافظ كلّها قبل العملية وبعدها. إن زاد
فالمنصة خلقت مالاً، وإن نقص فقد ضاع.
"""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.auction_utils import create_winner_reservation
from core.models import (
    AuctionListing, Bid, Category, CouponCollection, DeliveryCompany, DeliveryOrder, Dispute,
    FixedPriceListing, Item, ItemTransfer, ItemVariant, Location, Representative, Reservation,
    StorageFeePayment, StoreCoupon, User, UserStore, WithdrawalRequest, get_or_create_wallet,
)
from core.services.commission import platform_account
from core.services.disputes import resolve_dispute
from core.services.storage_fees import collect_due_storage_fees
from core.services.withdrawals import approve_withdrawal
from core.tasks import auto_complete_unclaimed_auction_reservations, process_expired_reservations
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db
P = D('100000')


def user(n, cash=0):
    u = User.objects.create_user(username=n, password='x')
    if cash:
        get_or_create_wallet(u).credit(D(cash))
    return u


def bal(u):
    return get_or_create_wallet(u).balance


def api(u):
    c = APIClient(); c.force_authenticate(user=u); return c


@pytest.fixture
def w():
    class W: pass
    x = W()
    x.seller, x.buyer, x.buyer2, x.rep = user('se'), user('bu', 5000000), user('b2', 5000000), user('rp')
    x.cat = Category.objects.create(name='c', slug='c')
    x.item = Item.objects.create(owner=x.seller, current_holder=x.seller, title='t', description='d',
                                 category=x.cat, condition='new', quantity=10, status='active')
    x.lst = FixedPriceListing.objects.create(item=x.item, price=P, deposit_percentage=10)
    x.loc = Location.objects.create(user=x.buyer, name='h', address_line1='a', city='b', state='baghdad',
                                    contact_person='p', contact_phone='07701234567')
    return x


def total():
    return sum(bal(u) for u in User.objects.all())


def res(w, buyer=None, **k):
    kw = dict(item=w.item, buyer=buyer or w.buyer, quantity=1, deposit_amount=D(10000), total_price=P,
              expires_at=timezone.now() + timedelta(days=1), platform_commission_percent=3,
              platform_commission_amount=D(3000), delivery_location=w.loc)
    kw.update(k)
    return Reservation.objects.create(**kw)


def test_wholesale_price_raised_after_request(w):
    assert api(w.buyer).post('/api/transfers/create_agreement/', {'item_id': w.item.id, 'quantity': 2},
                             format='json').status_code == 201
    t = ItemTransfer.objects.get()
    w.lst.price = D(1000000); w.lst.save()
    api(w.seller).post(f'/api/transfers/{t.id}/approve/')
    assert bal(w.buyer) == D(4800000)


@pytest.mark.parametrize('who', ['seller', 'buyer'])
def test_cancel_auction_win_returns_deposit(w, who):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=50000, minimum_increment=1,
                                      bid_deposit=10000, start_time=timezone.now() - timedelta(days=1),
                                      end_time=timezone.now(), status='ended')
    get_or_create_wallet(w.buyer).debit(D(10000))
    r = res(w, auction=a, total_price=D(50000))
    resp = api(getattr(w, who)).post(f'/api/reservations/{r.id}/cancel/')
    assert resp.status_code == 200, resp.data
    assert bal(w.buyer) + bal(w.seller) == D(5000000), (bal(w.buyer), bal(w.seller))


def test_dispute_resolved_twice_refunds_once(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', reason='other')
    before = total()
    for _ in range(2):
        try:
            resolve_dispute(Dispute.objects.get(pk=d.pk), resolution_action='refund_buyer')
        except Exception:
            pass
    assert total() == before + P, total() - before


def test_withdrawal_reviewed_twice(w):
    wal = get_or_create_wallet(w.buyer); wal.hold(D(100000)); wal.hold(D(100000))
    q = WithdrawalRequest.objects.create(user=w.buyer, amount=D(100000), card_last4='1234') if any(
        f.name == 'card_last4' for f in WithdrawalRequest._meta.fields) else WithdrawalRequest.objects.create(
        user=w.buyer, amount=D(100000))
    a, b = WithdrawalRequest.objects.get(pk=q.pk), WithdrawalRequest.objects.get(pk=q.pk)
    approve_withdrawal(a, reviewed_by=w.seller)
    try:
        approve_withdrawal(b, reviewed_by=w.seller)
    except Exception:
        pass
    assert bal(w.buyer) == D(4900000), bal(w.buyer)


def test_last_piece_sold_once(w):
    v = w.item.default_variant; v.quantity = 1; v.save()
    r1, r2 = res(w), res(w, buyer=w.buyer2)
    ok = 0
    for r in (r1, r2):
        resp = api(r.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'full'}, format='json')
        ok += resp.status_code == 200
    assert ok == 1, ok


def rep_res(w, rate):
    asg = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep,
                                        commission_percentage=rate, status='accepted')
    return res(w, representative=w.rep, representative_assignment=asg, commission_amount=P * rate / 100)


def test_rep_partial_refund_creates_no_money(w):
    r = rep_res(w, 30)
    before = total()
    with transaction.atomic():
        complete_reservation(r, 'full')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', reason='other')
    resolve_dispute(d, resolution_action='partial_refund', partial_refund_amount=D(80000))
    assert total() <= before, (total() - before, bal(w.rep), bal(w.seller))


def test_rep_100_percent_creates_no_money(w):
    r = rep_res(w, 100)
    before = total()
    with transaction.atomic():
        complete_reservation(r, 'full')
        release_reservation_payment(r)
    assert total() <= before, total() - before


def test_rep_sale_uses_variant_price(w):
    asg = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep,
                                        commission_percentage=10, status='accepted')
    w.item.options = [{'name': 's', 'values': ['XL']}]
    w.item.save()
    v = ItemVariant.objects.create(item=w.item, attributes={'s': 'XL'}, quantity=3, price=D(250000))
    resp = api(w.buyer).post(f'/api/representatives/{asg.id}/create_reservation_for_buyer/',
                             {'item_id': w.item.id, 'quantity': 1, 'variant_id': v.id}, format='json')
    assert resp.status_code == 201, resp.data
    r = Reservation.objects.get()
    assert r.variant_id == v.id and r.total_price == D(250000), (r.variant_id, r.total_price)


def order(w, r, st):
    c = DeliveryCompany.objects.create(name='dc', account=user('dc'))
    return DeliveryOrder.objects.create(reservation=r, direction='outbound', company=c, status=st)


def test_seller_cancel_stops_the_driver(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    o = order(w, r, 'assigned')
    assert api(w.seller).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    o.refresh_from_db()
    resp = [u for u in ('pick-up', 'pick_up')
            if api(o.company.account).post(f'/api/delivery-orders/{o.id}/{u}/').status_code == 200]
    assert not resp, ('driver could still pick up', o.status)


def test_buyer_cannot_cancel_once_shipped(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    order(w, r, 'picked_up')
    resp = api(w.buyer).post(f'/api/reservations/{r.id}/cancel/')
    assert resp.status_code != 200, (resp.status_code, bal(w.buyer))


def test_cancelled_order_gives_coupon_back(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    c = StoreCoupon.objects.create(store=st, code='A', amount=D(5000), min_order_amount=0)
    cc = CouponCollection.objects.create(coupon=c, user=w.buyer)
    r = res(w, applied_coupon=cc)
    assert api(w.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    resp = api(w.buyer).post('/api/reservations/create_reservation/',
                             {'item_id': w.item.id, 'quantity': 1, 'coupon_collection_id': cc.id,
                              'delivery_location_id': w.loc.id}, format='json')
    assert resp.status_code == 201, resp.data


def test_refund_after_release_takes_only_what_seller_got(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
        release_reservation_payment(r)
    get_or_create_wallet(w.seller).credit(D(50000))
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', reason='other')
    resolve_dispute(d, resolution_action='refund_buyer')
    assert (bal(w.seller), bal(platform_account())) == (D(50000), D(0)), (bal(w.seller), bal(platform_account()))


def test_negative_partial_refund_refused(w):
    r = res(w)
    before = total()
    with transaction.atomic():
        complete_reservation(r, 'full')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', reason='other')
    try:
        resolve_dispute(d, resolution_action='partial_refund', partial_refund_amount=D(-50000))
    except Exception:
        pass
    assert total() <= before, total() - before


def auction(w, deposit=5000, price=2000):
    item = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d',
                               category=w.cat, condition='new', quantity=1, status='active')
    return AuctionListing.objects.create(
        item=item, starting_price=1000, current_price=price, minimum_increment=1, bid_deposit=deposit,
        start_time=timezone.now() - timedelta(days=1), end_time=timezone.now(), status='ended')


def test_auction_deposit_above_price_is_not_swallowed(w):
    a = auction(w)
    before = total()
    get_or_create_wallet(w.buyer).debit(D(5000))
    bid = Bid.objects.create(auction=a, bidder=w.buyer, amount=2000, deposit_paid=True, is_winning=True)
    bid.refresh_from_db()
    a.refresh_from_db()
    r = create_winner_reservation(a, bid)
    assert (r.deposit_amount, bal(w.buyer)) == (D(2000), D(4998000))
    with transaction.atomic():
        complete_reservation(r, 'full')
        release_reservation_payment(r)
    assert total() == before


def auction_body(**k):
    return {'starting_price': '1000', 'minimum_increment': '100', 'bid_deposit': '500',
            'start_time': (timezone.now() + timedelta(hours=1)).isoformat(),
            'end_time': (timezone.now() + timedelta(days=2)).isoformat(), **k}


def test_auction_deposit_cannot_exceed_the_starting_price(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d',
                            category=w.cat, condition='new', quantity=1, status='draft')
    url = f'/api/items/{i.id}/create_auction/'
    assert api(w.seller).post(url, auction_body(bid_deposit='5000'), format='json').status_code == 400
    assert api(w.seller).post(url, auction_body(), format='json').status_code == 201
    a = AuctionListing.objects.get()
    patch = f'/api/items/{i.id}/update_auction/{a.id}/'
    assert api(w.seller).patch(patch, {'bid_deposit': '5000'}, format='json').status_code == 400
    assert api(w.seller).patch(patch, {'bid_deposit': '900'}, format='json').status_code == 200


def win(w, **k):
    a = auction(w, deposit=1000)
    get_or_create_wallet(w.buyer).debit(D(1000))
    return res(w, item=a.item, auction=a, total_price=D(2000), deposit_amount=D(1000),
               platform_commission_amount=D(60), **k)


def test_a_winner_backing_out_forfeits_the_deposit_to_the_seller(w):
    r = win(w)
    assert api(w.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert (bal(w.buyer), bal(w.seller)) == (D(4999000), D(1000))
    r.item.refresh_from_db()
    assert r.item.status == 'draft'


def test_paying_first_is_not_a_way_around_the_forfeit(w):
    r = win(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    assert api(w.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert (bal(w.buyer), bal(w.seller)) == (D(4999000), D(1000))


def test_the_seller_agreeing_returns_the_deposit(w):
    r = win(w, seller_cancellation_approved=True)
    assert api(w.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    assert (bal(w.buyer), bal(w.seller)) == (D(5000000), D(0))


def test_a_win_with_nothing_left_to_hand_over_returns_the_deposit(w):
    r = win(w, expires_at=timezone.now() - timedelta(hours=1))
    v = r.item.default_variant
    v.quantity = 0
    v.save()
    auto_complete_unclaimed_auction_reservations()
    r.refresh_from_db()
    assert (r.status, bal(w.buyer)) == ('cancelled', D(5000000))


def test_an_expired_order_gives_its_coupon_back(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    c = StoreCoupon.objects.create(store=st, code='A', amount=D(5000), min_order_amount=0)
    r = res(w, applied_coupon=CouponCollection.objects.create(coupon=c, user=w.buyer),
            expires_at=timezone.now() - timedelta(hours=1))
    process_expired_reservations()
    r.refresh_from_db()
    assert (r.status, r.applied_coupon) == ('expired', None)


def test_the_loser_of_the_last_piece_is_told_and_not_charged(w):
    v = w.item.default_variant
    v.quantity = 1
    v.save()
    r1, r2 = res(w), res(w, buyer=w.buyer2)
    with transaction.atomic():
        complete_reservation(r1, 'full')
    resp = api(w.buyer2).post(f'/api/reservations/{r2.id}/complete/', {'payment_type': 'full'}, format='json')
    assert (resp.status_code, resp.data['code'], bal(w.buyer2)) == (400, 'out_of_stock', D(5000000))


def released(w, rate):
    r = rep_res(w, rate)
    with transaction.atomic():
        complete_reservation(r, 'full')
        release_reservation_payment(r)
    return Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                                  title='t', description='d', reason='other')


def test_refund_after_release_takes_the_representatives_cut_from_them(w):
    before = total()
    resolve_dispute(released(w, 10), resolution_action='refund_buyer')
    assert total() == before
    assert (bal(w.seller), bal(w.rep), bal(platform_account())) == (0, 0, 0)


def test_partial_refund_after_release_is_shared(w):
    resolve_dispute(released(w, 10), resolution_action='partial_refund', partial_refund_amount=D(50000))
    assert (bal(w.seller), bal(w.rep), bal(platform_account())) == (D(43500), D(5000), D(1500))


def test_a_representative_who_spent_it_does_not_block_the_refund(w):
    d = released(w, 10)
    get_or_create_wallet(w.rep).debit(D(10000))
    get_or_create_wallet(w.seller).credit(D(10000))
    resolve_dispute(d, resolution_action='refund_buyer')
    assert (bal(w.buyer), bal(w.seller)) == (D(5000000), D(0))


def test_wholesale_retains_variant(w):
    w.item.options = [{'name': 's', 'values': ['XL']}]
    w.item.save()
    v = ItemVariant.objects.create(item=w.item, attributes={'s': 'XL'}, quantity=3, price=D(250000))
    resp = api(w.buyer).post('/api/transfers/create_agreement/',
                             {'item_id': w.item.id, 'quantity': 1, 'variant_id': v.id}, format='json')
    assert resp.status_code == 201, resp.data
    assert ItemTransfer.objects.get().variant_id == v.id


def test_wholesale_needs_the_option_chosen(w):
    w.item.options = [{'name': 's', 'values': ['XL']}]
    w.item.save()
    resp = api(w.buyer).post('/api/transfers/create_agreement/',
                             {'item_id': w.item.id, 'quantity': 1}, format='json')
    assert resp.status_code == 400


def stored(w, fee=1000, qty=3, periods=1, **k):
    now = timezone.now()
    copy = Item.objects.create(owner=w.buyer, current_holder=w.buyer, title='copy', description='d',
                               category=w.cat, condition='new', quantity=qty, status='active')
    return ItemTransfer.objects.create(
        item=w.item, from_user=w.seller, to_user=w.buyer, quantity=3, status='active', resulting_item=copy,
        wholesale_price_per_unit=P, storage_fee_type='flat_rate', storage_fee_amount=fee,
        storage_fee_period_days=30,
        next_storage_fee_due=now - timedelta(days=30 * (periods - 1), minutes=1), **k)


def test_storage_fee_is_charged_once_per_period(w):
    t = stored(w, periods=2)
    assert collect_due_storage_fees(t.id) == 2
    assert collect_due_storage_fees(t.id) == 0
    t.refresh_from_db()
    assert (bal(w.buyer), bal(w.seller), t.total_storage_fees_paid) == (D(4998000), D(2000), D(2000))


def test_no_goods_no_storage_fee(w):
    t = stored(w, qty=0)
    collect_due_storage_fees(t.id)
    assert bal(w.seller) == 0
    assert StorageFeePayment.objects.get().status == 'waived'


def test_an_unpaid_storage_fee_waits_and_does_not_stack(w):
    t = stored(w, fee=6000000, periods=2)
    for _ in range(3):
        collect_due_storage_fees(t.id)
    assert [p.status for p in StorageFeePayment.objects.all()] == ['overdue']
    get_or_create_wallet(w.buyer).credit(D(1000000))
    assert collect_due_storage_fees(t.id) == 1


def test_storage_fees_stop_when_the_term_ends(w):
    t = stored(w, agreement_end_date=timezone.now() - timedelta(days=5))
    collect_due_storage_fees(t.id)
    t.refresh_from_db()
    assert (bal(w.seller), t.next_storage_fee_due) == (0, None)
