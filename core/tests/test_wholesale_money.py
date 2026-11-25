"""مال الجملة المحجوز، وحدود الاحتيال وإيقاف الخدمة (2026-11-25)."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuctionListing, DeliveryCompany, DeliveryOrder, Dispute, FixedPriceListing, Item, ItemTransfer,
    ItemVariant, Location, Representative, Reservation, StoreBadge, User, UserStore,
    get_or_create_wallet,
)
from core.services.commission import platform_account
from core.services.refusals import deposit_refused_to
from core.services.store_badges import grant_badge
from core.services.wholesale import (
    HANDOVER_DAYS, NOTICE, goods_left, settle_claimed_handovers,
)
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def ask(w, n=4, **kw):
    return ItemTransfer.objects.create(item=w.item, from_user=w.seller, to_user=w.buyer, quantity=n,
                                       wholesale_price_per_unit=P, **kw)


def approve(w, t):
    x = api(w.seller).post(f'/api/transfers/{t.id}/approve/', {'escrow_ack': True}, format='json')
    assert x.status_code == 200, x.data
    t.refresh_from_db()
    return t


def act(u, t, what, body=None):
    x = api(User.objects.get(pk=u.pk)).post(f'/api/transfers/{t.id}/{what}/', body or {}, format='json')
    t.refresh_from_db()
    return x


def everything():
    """Wallets plus what is held: must never change but by a top-up."""
    return total() + sum(t.held_amount for t in ItemTransfer.objects.all())


def wholesale_store(u):
    s, _ = UserStore.objects.get_or_create(user=u, defaults={'name': 's', 'is_active': True})
    return grant_badge(s, StoreBadge.KIND_WHOLESALE, by=u)


# ---- who is paid when

def test_money_is_held_until_goods_move(w):
    before = everything()
    t = approve(w, ask(w))
    assert bal(w.seller) == 0 and bal(w.buyer) == 5000000 - 4 * P
    assert t.escrow and t.held_quantity == 4 and everything() == before


def test_a_wholesale_store_is_paid_now(w):
    wholesale_store(w.seller)
    before = everything()
    t = approve(w, ask(w))
    assert not t.escrow and t.held_quantity == 0
    assert bal(w.seller) == 4 * P - t.platform_commission_amount and everything() == before


def test_badge_shows_and_only_admins_grant_it(w):
    from core.services.store_badges import BadgeError, badges_for_store
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    with pytest.raises(BadgeError):
        grant_badge(s, StoreBadge.KIND_WHOLESALE)
    grant_badge(s, StoreBadge.KIND_WHOLESALE, by=w.seller)
    assert badges_for_store(s)[0] == 'wholesale'


def test_the_notice_comes_before_the_first_approval_once(w):
    t = ask(w, 1)
    seen = api(w.seller).get('/api/transfers/received_requests/').data['data'][0]
    assert seen['escrow_notice'] == NOTICE and seen['pays_immediately'] is False
    # An older build sends no flag: told instead, and the second tap works.
    x = act(w.seller, t, 'approve')
    assert x.status_code == 400 and x.data['code'] == 'escrow_notice' and t.status == 'pending'
    assert api(User.objects.get(pk=w.seller.pk)).get('/api/transfers/received_requests/').data['data'][0]['escrow_notice'] is None
    assert act(w.seller, t, 'approve').status_code == 200


def test_a_wholesale_store_gets_no_notice(w):
    wholesale_store(w.seller)
    assert act(w.seller, ask(w, 1), 'approve').status_code == 200


# ---- released piece by piece

def test_confirming_receipt_pays_for_those_pieces(w):
    t = approve(w, ask(w, 3))
    before = everything()
    assert act(w.seller, t, 'confirm_received').status_code == 403
    assert act(w.buyer, t, 'confirm_received', {'quantity': 4}).status_code == 400
    assert act(w.buyer, t, 'confirm_received', {'quantity': 1}).status_code == 200
    assert t.held_quantity == 2 and t.status == 'active'
    assert act(w.buyer, t, 'confirm_received').status_code == 200
    assert t.held_quantity == 0 and t.status == 'completed'
    assert bal(w.seller) == 3 * P - t.platform_commission_amount
    assert everything() == before
    assert act(w.buyer, t, 'confirm_received').status_code == 400


def test_commission_parts_add_up(w):
    t = approve(w, ask(w, 3))
    ItemTransfer.objects.filter(pk=t.pk).update(platform_commission_amount=D('10000.00'))
    before = bal(platform_account())
    for _ in range(3):
        act(w.buyer, t, 'confirm_received', {'quantity': 1})
    assert bal(platform_account()) - before == D('10000.00')
    assert bal(w.seller) == 3 * P - D('10000.00')


def sell(w, item, buyer, n=1):
    FixedPriceListing.objects.get_or_create(item=item, defaults={'price': P, 'deposit_percentage': 10})
    Item.objects.filter(pk=item.pk).update(status='active')
    item = Item.objects.get(pk=item.pk)
    r = Reservation.objects.create(item=item, buyer=buyer, quantity=n, deposit_amount=P / 10,
                                   total_price=P * n, expires_at=timezone.now() + timedelta(days=1))
    with transaction.atomic():
        complete_reservation(r, 'full')
    return r


def test_selling_on_pays_the_first_seller(w):
    t = approve(w, ask(w, 3))
    r = sell(w, t.resulting_item, w.buyer2, 2)
    t.refresh_from_db(); assert t.held_quantity == 3
    with transaction.atomic():
        release_reservation_payment(r)
    t.refresh_from_db()
    assert t.released_quantity == 2 and bal(w.seller) > 0


def test_it_goes_up_a_chain_of_resales(w):
    t = approve(w, ask(w, 3))
    second = user('third', 5000000)
    FixedPriceListing.objects.create(item=t.resulting_item, price=P, deposit_percentage=10)
    Item.objects.filter(pk=t.resulting_item_id).update(status='active')
    t2 = ItemTransfer.objects.create(item=Item.objects.get(pk=t.resulting_item_id), from_user=w.buyer,
                                     to_user=second, quantity=2, wholesale_price_per_unit=P)
    act(w.buyer, t2, 'approve', {'escrow_ack': True})
    with transaction.atomic():
        goods_left(t2.resulting_item, 1)
    t.refresh_from_db(); t2.refresh_from_db()
    assert (t.released_quantity, t2.released_quantity) == (1, 1)


# ---- the ways out

def test_refund_returns_money_and_stock(w):
    before = everything()
    t = approve(w, ask(w, 4))
    assert act(w.seller, t, 'accept_refund').status_code == 400
    assert act(w.buyer, t, 'request_refund').status_code == 200
    assert act(w.buyer, t, 'accept_refund').status_code == 403
    assert act(w.seller, t, 'accept_refund').status_code == 200
    w.item.refresh_from_db()
    assert w.item.quantity == 10 and bal(w.buyer) == 5000000 and t.status == 'cancelled'
    assert Item.objects.get(pk=t.resulting_item_id).quantity == 0 and everything() == before


def test_pieces_already_sold_are_not_refunded(w):
    t = approve(w, ask(w, 4))
    r = sell(w, t.resulting_item, w.buyer2, 1)
    # Stock the reseller typed in does not make the sold piece refundable.
    ItemVariant.objects.filter(item=t.resulting_item).update(quantity=50)
    act(w.buyer, t, 'request_refund')
    assert act(w.seller, t, 'accept_refund').status_code == 200
    assert t.refunded_quantity == 3 and t.held_quantity == 1 and t.status == 'active'
    with transaction.atomic():
        release_reservation_payment(r)
    t.refresh_from_db()
    assert t.held_quantity == 0 and bal(w.seller) > 0


def test_an_unanswered_handover_pays_after_a_week(w):
    t = approve(w, ask(w, 2))
    assert act(w.seller, t, 'claim_handover').status_code == 200
    assert settle_claimed_handovers() == 0
    assert settle_claimed_handovers(timezone.now() + timedelta(days=HANDOVER_DAYS, minutes=1)) == 1
    t.refresh_from_db()
    assert t.held_quantity == 0 and bal(w.seller) == 2 * P - t.platform_commission_amount


def test_an_objection_stops_the_clock(w):
    t = approve(w, ask(w, 2))
    act(w.seller, t, 'claim_handover')
    act(w.buyer, t, 'request_refund')
    assert settle_claimed_handovers(timezone.now() + timedelta(days=30)) == 0
    assert act(w.seller, t, 'claim_handover').status_code == 400


def test_an_agreement_with_held_money_is_not_closed(w):
    t = approve(w, ask(w, 2))
    assert act(w.seller, t, 'cancel').status_code == 400
    assert act(w.seller, t, 'complete').status_code == 400
    assert act(w.buyer, t, 'complete').status_code == 400


def test_agreements_from_before_stay_as_they_were(w):
    t = ask(w, 2, status='active')
    assert t.held_quantity == 0 and act(w.seller, t, 'complete').status_code == 200


def test_wallet_tells_the_wholesale_story(w):
    t = approve(w, ask(w, 2))
    act(w.buyer, t, 'confirm_received', {'quantity': 1})
    mine = api(w.seller).get('/api/wallet/activity/').data['data']
    e = [x for x in mine['entries'] if x['kind'] == 'wholesale'][0]
    assert e['direction'] == 'in' and D(mine['escrow']['earnings_waiting']) > 0
    theirs = api(w.buyer).get('/api/wallet/activity/').data['data']
    assert D(theirs['escrow']['paid_waiting']) == P


# ---- accounts

def test_neither_side_leaves_an_open_wholesale_sale(w):
    t = approve(w, ask(w, 2))
    for u in (w.seller, w.buyer):
        get_or_create_wallet(u); User.objects.get(pk=u.pk).wallet.__class__.objects.filter(user=u).update(balance=0)
        x = api(User.objects.get(pk=u.pk)).post('/api/users/delete-account/', {'current_password': 'x'}, format='json')
        assert x.status_code == 400 and 'جملة' in x.data['message'], x.data


def buy(w, n=1):
    return api(w.buyer).post('/api/reservations/create_reservation/', {
        'item_id': w.item.id, 'quantity': n, 'delivery_location_id': w.loc.id}, format='json')


def test_goods_of_a_gone_seller_are_not_for_sale(w):
    r = res(w)
    User.objects.filter(pk=w.seller.pk).update(is_active=False)
    assert buy(w).status_code == 400
    assert APIClient().get('/api/listings/fixed/').json()['results'] == []
    assert APIClient().get('/api/items/').json()['results'] == []
    x = api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'full', 'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 400 and bal(w.buyer) == 5000000


def test_deleting_the_account_takes_the_goods_down(w):
    x = api(w.seller).post('/api/users/delete-account/', {'current_password': 'x'}, format='json')
    w.item.refresh_from_db()
    assert x.status_code == 200 and w.item.status == 'archived'


@pytest.mark.parametrize('name', ['الشورجة', 'الشـورجة', 'الدعم الفني', 'Shorjeh Support', 'إدارة الشورجة', 'SH0RJEH', 'الدَّعم  الفنّي'])
def test_nobody_poses_as_the_platform(w, name):
    assert api(user('p')).post('/api/stores/', {'name': name}).status_code == 400
    assert api(w.buyer).patch(f'/api/users/{w.buyer.id}/', {'first_name': name}, format='json').status_code == 400


@pytest.mark.parametrize('name', ['أقمشة الشورجة', 'عطور سوق الشورجة', 'علي للأدوات', 'Admiral Shoes'])
def test_a_shop_in_the_market_keeps_its_name(w, name):
    x = api(user('p')).post('/api/stores/', {'name': name})
    assert x.status_code != 400 or 'name' not in (x.data.get('errors') or x.data), x.data


# ---- withdrawing

CARD = {'account_number': '6330123456789012', 'holder_name': 'x'}


def withdraw(u, amount):
    return api(User.objects.get(pk=u.pk)).post('/api/wallet/withdraw/', {'amount': str(amount), **CARD}, format='json')


def verify(u, **kw):
    User.objects.filter(pk=u.pk).update(phone='+9647801234567', phone_verified=True, **kw)


def test_withdrawing_needs_a_verified_settled_phone(w):
    assert withdraw(w.buyer, 1000).status_code == 400
    verify(w.buyer, phone_changed_at=timezone.now() - timedelta(hours=47))
    assert withdraw(w.buyer, 1000).status_code == 400
    verify(w.buyer, phone_changed_at=timezone.now() - timedelta(hours=49))
    assert withdraw(w.buyer, 1000).status_code == 201


def test_disputable_earnings_are_spendable_not_withdrawable(w):
    verify(w.seller)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full'); release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed')
    assert bal(w.seller) > 0
    assert withdraw(w.seller, 1000).status_code == 400
    me = api(User.objects.get(pk=w.seller.pk)).get('/api/wallet/me/').data['data']
    assert D(me['withdrawable_balance']) == 0 and D(me['available_balance']) > 0
    Reservation.objects.filter(pk=r.pk).update(
        completed_at=timezone.now() - timedelta(days=60), payment_released_at=timezone.now() - timedelta(days=60))
    assert withdraw(w.seller, 1000).status_code == 201


# ---- auctions, disputes, refusals, rep links

def test_an_absurd_bid_is_refused(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat,
                            condition='new', quantity=1, status='active')
    a = AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=100, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1), status='active')
    bid = lambda n: api(w.buyer).post(f'/api/auctions/{a.id}/place_bid/', {'amount': str(n)}, format='json').status_code
    assert bid(1001001) == 400 and bid(1001000) in (200, 201)


def test_a_dispute_needs_an_order_and_is_not_filed_for_ever(w):
    body = {'item_id': w.item.id, 'title': 't', 'description': 'd'}
    file = lambda **k: api(w.buyer).post('/api/disputes/create_dispute/', {**body, **k}, format='json').status_code
    assert file() == 400
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    for _ in range(2):
        assert file(reservation_id=r.id) == 201
        assert file(reservation_id=r.id) == 400
        Reservation.objects.filter(pk=r.pk).update(status='completed')
        Dispute.objects.update(status='closed')
    assert file(reservation_id=r.id) == 400


def refuse(w, n):
    co = DeliveryCompany.objects.first() or DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    out = []
    for _ in range(n):
        r = res(w)
        out.append(DeliveryOrder.objects.create(reservation=r, company=co, direction='outbound', status='refused',
                                                refused_at=timezone.now()))
    return out


def test_a_repeat_refuser_pays_in_full(w):
    refuse(w, 2)
    assert not deposit_refused_to(w.buyer)
    orders = refuse(w, 1)
    assert deposit_refused_to(w.buyer)
    Reservation.objects.filter(buyer=w.buyer).update(status='cancelled')
    r = Reservation.objects.get(pk=buy(w).data['data']['id'])
    pay = lambda kind: api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': kind}, format='json')
    x = pay('deposit')
    assert x.status_code == 400 and x.data['code'] == 'full_payment_only'
    assert pay('full').status_code == 200
    # Being right about one of them un-counts it.
    Dispute.objects.create(item=w.item, reservation=orders[0].reservation, complainant=w.buyer, defendant=w.seller,
                           title='t', description='d', status='resolved', resolution_action='refund_buyer')
    assert not deposit_refused_to(w.buyer)


def test_a_rep_link_holds_little_for_a_short_while(w):
    a = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep, commission_percentage=1, status='accepted')
    ItemVariant.objects.filter(item=w.item).update(quantity=50)
    go = lambda n: api(w.buyer).post(f'/api/representatives/{a.id}/create_reservation_for_buyer/', {'item_id': w.item.id, 'quantity': n}, format='json')
    assert go(11).status_code == 400
    assert go(10).status_code == 201
    assert Reservation.objects.get().expires_at < timezone.now() + timedelta(minutes=31)


# ---- sizes

def test_sizes_are_bounded(w):
    c = api(w.seller); c.raise_request_exception = False
    vals = [str(i) for i in range(101)]
    x = c.patch(f'/api/items/{w.item.id}/', {'options': [{'name': 's', 'values': vals}],
                'variants': [{'attributes': {'s': v}, 'quantity': 1} for v in vals]}, format='json')
    assert x.status_code == 400
    assert c.patch(f'/api/items/{w.item.id}/', {'description': 'x' * 5001}, format='json').status_code == 400
    assert c.patch(f'/api/items/{w.item.id}/', {'description': 'x' * 5000}, format='json').status_code == 200


def test_price_times_quantity_is_not_a_500(w):
    ItemVariant.objects.filter(item=w.item).update(quantity=2_000_000)
    FixedPriceListing.objects.filter(pk=w.lst.pk).update(price=D('900000000000'))
    w.buyer_client = api(w.buyer); w.buyer_client.raise_request_exception = False
    x = w.buyer_client.post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 2_000_000,
                            'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 400


def test_nearby_is_bounded(w):
    loc = Location.objects.create(user=w.seller, name='x', address_line1='a', city='b', state='baghdad', contact_person='p',
                                  contact_phone='07701234567', latitude='33.3', longitude='44.4')
    Item.objects.bulk_create([Item(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat,
                                   condition='new', quantity=1, status='active', current_location=loc) for _ in range(120)])
    x = APIClient().get('/api/locations/nearby_items/', {'lat': '33.3', 'lng': '44.4', 'radius': '99999'})
    assert len(x.json()['data']) == 100


def test_reel_uploads_are_bounded(w, settings):
    from unittest.mock import patch
    UserStore.objects.create(user=w.seller, name='s', is_active=True)
    settings.REEL_MAX_UPLOADS_PER_DAY = 2
    go = lambda **k: api(w.seller).post('/api/reels/request_upload_url/', {'content_type': 'video/mp4', **k}, format='json').status_code
    with patch('core.aws_utils.build_presigned_put_url', return_value='u') as sign:
        assert go(content_length=100 * 1024 * 1024 + 1) == 400
        assert go(content_length=5000) == 201 and sign.call_args[0][2] == 5000
        assert go() == 201 and go() == 400


# ---- OTP

def test_strangers_cannot_use_up_the_days_codes(settings):
    from unittest.mock import patch
    settings.OTP_DAILY_SEND_CAP, settings.OTP_MEMBERS_SHARE = 10, 0.4
    User.objects.create_user(username='m', password='x', phone='07809999999')
    n = iter(range(1000))
    send = lambda phone: APIClient(REMOTE_ADDR='10.9.%d.1' % next(n)).post('/api/auth/phone/send-code/', {'phone': phone}, format='json').status_code
    with patch('core.otp_views.send_otp', return_value='m'):
        assert [send('0770000%04d' % i) for i in range(8)].count(200) == 6
        assert send('07809999999') == 200


def test_one_address_has_a_daily_share(settings):
    from unittest.mock import patch
    settings.OTP_STRANGERS_PER_IP_PER_DAY = 2
    with patch('core.otp_views.send_otp', return_value='m'):
        codes = [APIClient().post('/api/auth/phone/send-code/', {'phone': '0770000%04d' % i}, format='json').status_code for i in range(3)]
    assert codes == [200, 200, 503]
