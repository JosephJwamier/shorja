"""طلبات متزامنة حقيقية: كل اختبار ضغطتان في اللحظة نفسها (2026-11-26)."""
import threading
from datetime import timedelta
from decimal import Decimal as D
import pytest
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.utils import timezone
from core.models import *
from core.models import get_or_create_wallet
from core.tests.test_money_faults import P, api, bal, res, total, user
from core.wallet_utils import complete_reservation, release_reservation_payment
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def clean():
    cache.clear(); yield; cache.clear()


@pytest.fixture
def w():
    class W: pass
    x = W()
    x.seller, x.buyer, x.buyer2, x.rep = user('se'), user('bu', 5000000), user('b2', 5000000), user('rp')
    x.cat = Category.objects.create(name='c', slug='c')
    x.item = Item.objects.create(owner=x.seller, current_holder=x.seller, title='t', description='d', category=x.cat, condition='new', quantity=10, status='active')
    x.lst = FixedPriceListing.objects.create(item=x.item, price=P, deposit_percentage=10)
    x.loc = Location.objects.create(user=x.buyer, name='h', address_line1='a', city='b', state='baghdad', contact_person='p', contact_phone='07701234567')
    x.loc2 = Location.objects.create(user=x.buyer2, name='h', address_line1='a', city='b', state='baghdad', contact_person='p', contact_phone='07701234567')
    return x


def race(*calls):
    gate = threading.Barrier(len(calls)); out = [None] * len(calls)

    def go(n, f):
        gate.wait()
        try:
            out[n] = f()
        except Exception as e:
            out[n] = repr(e)[:120]
        finally:
            connections.close_all()
    ts = [threading.Thread(target=go, args=(n, f)) for n, f in enumerate(calls)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def post(u, url, body=None):
    def f():
        c = api(User.objects.get(pk=u.pk)); c.raise_request_exception = False
        return c.post(url, body or {}, format='json').status_code
    return f


def world():
    return total() + sum(t.held_amount for t in ItemTransfer.objects.all())


def held(w, n=4):
    t = ItemTransfer.objects.create(item=w.item, from_user=w.seller, to_user=w.buyer, quantity=n, wholesale_price_per_unit=P)
    post(w.seller, f'/api/transfers/{t.id}/approve/', {'escrow_ack': True})()
    return t


def test_confirm_confirm(w):
    t = held(w); b = world(); u = f'/api/transfers/{t.id}/confirm_received/'
    r = race(*[post(w.buyer, u)] * 4)
    assert world() == b and 500 not in r, (r, world() - b)


def test_confirm_vs_refund(w):
    t = held(w); b = world()
    post(w.buyer, f'/api/transfers/{t.id}/request_refund/')()
    r = race(post(w.buyer, f'/api/transfers/{t.id}/confirm_received/'), post(w.seller, f'/api/transfers/{t.id}/accept_refund/'))
    assert world() == b and 500 not in r, (r, world() - b)


def test_approve_approve(w):
    t = ItemTransfer.objects.create(item=w.item, from_user=w.seller, to_user=w.buyer, quantity=4, wholesale_price_per_unit=P)
    r = race(*[post(w.seller, f'/api/transfers/{t.id}/approve/', {'escrow_ack': True})] * 4)
    assert bal(w.buyer) == 5000000 - 4 * P and 500 not in r, (r, bal(w.buyer))


def test_last_piece(w):
    ItemVariant.objects.filter(item=w.item).update(quantity=1); Item.objects.update(quantity=1)
    a = Reservation.objects.create(item=w.item, buyer=w.buyer, quantity=1, deposit_amount=P / 10, total_price=P, delivery_location=w.loc, expires_at=timezone.now() + timedelta(days=1))
    b = Reservation.objects.create(item=w.item, buyer=w.buyer2, quantity=1, deposit_amount=P / 10, total_price=P, delivery_location=w.loc2, expires_at=timezone.now() + timedelta(days=1))
    r = race(post(w.buyer, f'/api/reservations/{a.id}/complete/', {'payment_type': 'full'}), post(w.buyer2, f'/api/reservations/{b.id}/complete/', {'payment_type': 'full'}))
    assert sorted(r) == [200, 400], r


def test_one_offer_two_orders(w):
    ch = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=ch, sender=w.buyer, message_type='offer', offer_amount=5000, offer_quantity=1, offer_status='accepted', offer_accepted_at=timezone.now(), content='x')
    r = race(*[post(w.buyer, '/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'offer_message_id': m.id})] * 4)
    assert Reservation.objects.filter(applied_offer=m).count() == 1 and 500 not in r, r


def test_one_coupon_two_orders(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    cc = CouponCollection.objects.create(coupon=StoreCoupon.objects.create(store=s, code='A', amount=5000), user=w.buyer)
    r = race(*[post(w.buyer, '/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'coupon_collection_id': cc.id})] * 4)
    assert Reservation.objects.filter(applied_coupon=cc).count() == 1 and 500 not in r, r


def test_withdraw_twice(w):
    User.objects.filter(pk=w.buyer.pk).update(phone='+9647801234567', phone_verified=True)
    r = race(*[post(w.buyer, '/api/wallet/withdraw/', {'amount': '5000000', 'account_number': '6330123456789012', 'holder_name': 'x'})] * 4)
    assert r.count(201) == 1 and 500 not in r, r


def paid(w):
    r = Reservation.objects.create(item=w.item, buyer=w.buyer, quantity=1, deposit_amount=P / 10, total_price=P, delivery_location=w.loc, expires_at=timezone.now() + timedelta(days=1))
    with transaction.atomic():
        complete_reservation(r, 'full')
    return r


def test_release_vs_dispute(w):
    for _ in range(3):
        r = paid(w)
        o = race(post(w.buyer, f'/api/reservations/{r.id}/approve_and_release_payment/'),
                 post(w.buyer, '/api/disputes/create_dispute/', {'item_id': w.item.id, 'reservation_id': r.id, 'title': 't', 'description': 'd'}))
        r.refresh_from_db()
        assert 500 not in o and not (r.status == 'confirmed' and Dispute.objects.filter(reservation=r, status='open').exists()), (o, r.status)


def test_cancel_vs_release(w):
    r = paid(w); b = total()
    o = race(post(w.buyer, f'/api/reservations/{r.id}/approve_and_release_payment/'), post(w.seller, f'/api/reservations/{r.id}/cancel/'))
    r.refresh_from_db()
    assert 500 not in o and total() == b + P, (o, r.status, total() - b)


def test_bids(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='active')
    a = AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=500, start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    r = race(*[post(u, f'/api/auctions/{a.id}/place_bid/', {'amount': str(n)}) for u, n in ((w.buyer, 2000), (w.buyer2, 2000), (w.buyer, 3000), (w.buyer2, 3000))])
    assert Bid.objects.filter(auction=a, is_winning=True).count() == 1 and 500 not in r, r
    assert bal(w.buyer) >= 5000000 - 500 and bal(w.buyer2) >= 5000000 - 500


@pytest.mark.parametrize('what', ['follow', 'like', 'favorite', 'block', 'review'])
def test_double_taps(w, what):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    reel = Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    ch = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    r = paid(w)
    with transaction.atomic():
        release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed')
    url, body = {'follow': (f'/api/stores/{s.id}/follow/', {}), 'like': (f'/api/reels/{reel.id}/like/', {}), 'favorite': (f'/api/items/{w.item.id}/favorite/', {}),
                 'block': (f'/api/chats/{ch.id}/block/', {}), 'review': ('/api/product-reviews/', {'item': w.item.id, 'score': 5, 'comment': 'x'})}[what]
    o = race(*[post(w.buyer, url, body)] * 5)
    reel.refresh_from_db()
    assert 500 not in o and reel.likes_count <= 1 and not any(isinstance(x, str) for x in o), (o, reel.likes_count)


def test_same_phone_twice():
    from rest_framework.test import APIClient
    body = {'first_name': 'a', 'last_name': 'b', 'phone': '07701110000', 'password': 'Zx9!kQ2#long', 'password2': 'Zx9!kQ2#long'}

    def f():
        c = APIClient(); c.raise_request_exception = False
        return c.post('/api/auth/register/', body, format='json').status_code
    r = race(f, f, f)
    assert r.count(201) == 1 and 500 not in r, r


def test_dispute_twice_resolved(w):
    from core.services.disputes import resolve_dispute
    r = paid(w); b = total()
    Reservation.objects.filter(pk=r.pk).update(status='disputed')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller, title='t', description='d')
    race(*[lambda: resolve_dispute(Dispute.objects.get(pk=d.pk), resolution_action='refund_buyer')] * 3)
    assert total() == b + P


def test_chat_created_once(w):
    o = race(*[post(w.buyer, '/api/chats/create_or_get/', {'item_id': w.item.id})] * 4)
    assert Chat.objects.filter(item=w.item, buyer=w.buyer).count() == 1 and 500 not in o, (o, Chat.objects.count())


def test_store_chat_created_once(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    o = race(*[post(w.buyer, '/api/chats/create_or_get_store/', {'store_id': s.id})] * 4)
    assert Chat.objects.filter(store=s, buyer=w.buyer).count() == 1 and 500 not in o, (o, Chat.objects.count())


def test_support_chat_created_once(w):
    from core.services.support import SUPPORT_USERNAME
    User.objects.get_or_create(username=SUPPORT_USERNAME)
    o = race(*[post(w.buyer, '/api/chats/create_or_get_support/')] * 4)
    assert Chat.objects.filter(buyer=w.buyer).count() == 1 and 500 not in o, (o, Chat.objects.count())


def test_wallet_created_once():
    u = User.objects.create_user(username='fresh', password='x')
    o = race(*[post(u, '/api/wallet/withdraw/', {'amount': '5', 'account_number': '6330123456789012', 'holder_name': 'x'})] * 4)
    assert 500 not in o, o


def test_two_wholesale_requests(w):
    o = race(*[post(w.buyer, '/api/transfers/create_agreement/', {'item_id': w.item.id, 'quantity': 2})] * 4)
    assert ItemTransfer.objects.count() == 1 and 500 not in o, (o, ItemTransfer.objects.count())


def test_two_open_orders_by_rep(w):
    a = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep, commission_percentage=1, status='accepted')
    o = race(*[post(w.buyer, f'/api/representatives/{a.id}/create_reservation_for_buyer/', {'item_id': w.item.id, 'quantity': 2})] * 4)
    assert Reservation.objects.count() == 1 and 500 not in o, (o, Reservation.objects.count())


def test_resale_release_vs_confirm(w):
    """A customer's order releasing while the reseller confirms the rest."""
    t = held(w, 4)
    i = ItemTransfer.objects.get(pk=t.pk).resulting_item
    FixedPriceListing.objects.create(item=i, price=P, deposit_percentage=10)
    Item.objects.filter(pk=i.pk).update(status='active')
    r = Reservation.objects.create(item=Item.objects.get(pk=i.pk), buyer=w.buyer2, quantity=2, deposit_amount=P / 10, total_price=2 * P, delivery_location=w.loc2, expires_at=timezone.now() + timedelta(days=1))
    with transaction.atomic():
        complete_reservation(r, 'full')
    b = world()
    o = race(post(w.buyer2, f'/api/reservations/{r.id}/approve_and_release_payment/'), post(w.buyer, f'/api/transfers/{t.id}/confirm_received/'))
    assert 500 not in o and world() == b + 2 * P, (o, world() - b)


def test_a_duplicate_chat_does_not_break_the_button(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    race(*[post(w.buyer, '/api/chats/create_or_get_store/', {'store_id': s.id})] * 4)
    assert post(w.buyer, '/api/chats/create_or_get_store/', {'store_id': s.id})() == 200


def test_a_duplicate_support_chat_does_not_break_the_button(w):
    from core.services.support import SUPPORT_USERNAME
    User.objects.get_or_create(username=SUPPORT_USERNAME)
    race(*[post(w.buyer, '/api/chats/create_or_get_support/')] * 4)
    assert post(w.buyer, '/api/chats/create_or_get_support/')() == 200


def test_two_requests_do_not_charge_twice(w):
    race(*[post(w.buyer, '/api/transfers/create_agreement/', {'item_id': w.item.id, 'quantity': 2})] * 4)
    for t in ItemTransfer.objects.all():
        post(w.seller, f'/api/transfers/{t.id}/approve/', {'escrow_ack': True})()
    assert bal(w.buyer) == 5000000 - 2 * P, bal(w.buyer)


@pytest.mark.parametrize('what', ['dispute', 'report', 'storereview', 'location', 'store', 'rep', 'collect'])
def test_more_double_taps(w, what):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    r = paid(w)
    cp = StoreCoupon.objects.create(store=s, code='A', amount=5000)
    with transaction.atomic():
        release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed')
    url, body, model, most = {
        'dispute': ('/api/disputes/create_dispute/', {'item_id': w.item.id, 'reservation_id': r.id, 'title': 't', 'description': 'd'}, Dispute, 1),
        'report': ('/api/reports/create_report/', {'target_type': 'item', 'target_id': w.item.id, 'reason': Report.REASON_CHOICES[0][0]}, Report, 1),
        'storereview': ('/api/store-reviews/', {'store': s.id, 'score': 5, 'comment': 'x'}, StoreReview, 1),
        'location': ('/api/locations/', {'name': 'n', 'address_line1': 'a', 'city': 'b', 'state': 'baghdad', 'contact_person': 'p', 'contact_phone': '07701234567', 'is_primary': True, 'location_type': 'user_address'}, Location, 9),
        'store': ('/api/stores/', {'name': 'mine'}, UserStore, 2),
        'rep': ('/api/representatives/create_request/', {'item_id': w.item.id, 'commission_percentage': '5'}, Representative, 1),
        'collect': (f'/api/stores/{s.id}/coupons/{cp.id}/collect/', {}, CouponCollection, 1),
    }[what]
    if what == 'store':
        def f():
            c = api(User.objects.get(pk=w.buyer.pk)); c.raise_request_exception = False
            return c.post(url, body).status_code
        o = race(*[f] * 4)
    else:
        o = race(*[post(w.buyer, url, body)] * 4)
    assert 500 not in o and model.objects.count() <= most and Location.objects.filter(user=w.buyer, is_primary=True).count() <= 1, (o, model.objects.count(), Location.objects.filter(is_primary=True).count())


def test_old_duplicates_are_merged(w):
    import importlib
    from django.apps import apps
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    cs = [Chat.objects.create(store=s, buyer=w.buyer, seller=w.seller) for _ in range(3)]
    other = Chat.objects.create(store=s, buyer=w.buyer2, seller=w.seller)
    for c in cs + [other]:
        Message.objects.create(chat=c, sender=w.buyer, message_type='user', content='x')
    importlib.import_module('core.migrations.0093_merge_duplicate_chats').merge(apps, None)
    assert Message.objects.filter(chat=cs[0]).count() == 3 and set(Chat.objects.all()) == {cs[0], other}


def test_equal_prices_do_not_repeat_across_pages(w):
    for _ in range(45):
        i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='active')
        FixedPriceListing.objects.create(item=i, price=P, deposit_percentage=10)
    seen, url = [], '/api/listings/fixed/?ordering=price'
    while url:
        d = api(w.buyer).get(url).json()
        seen += [x['id'] for x in d['results']]; url = d.get('next')
    assert len(seen) == len(set(seen)) == 46
