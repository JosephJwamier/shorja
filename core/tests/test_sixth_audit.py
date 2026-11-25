"""ما كشفه الفحص السادس (2026-11-25)."""
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.delivery_utils import create_outbound_delivery_order, create_return_delivery_order, execute_return_refund
from core.models import *
from core.models import get_or_create_wallet
from core.services.disputes import resolve_dispute
from core.services.store_badges import grant_badge, revoke_badge
from core.services.wholesale import needs_notice
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.tests.test_wholesale_money import act, approve, ask, sell, verify, withdraw, wholesale_store
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db
PW = 'Zx9!kQ2#long'


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def co(fee=5000):
    return DeliveryCompany.objects.first() or DeliveryCompany.objects.create(
        name='dc', account=user('dc'), is_active=True, intra_governorate_fee=fee, inter_governorate_fee=3 * fee)


def free_order(w, n=1):
    w.lst.deposit_percentage = 0; w.lst.save()
    r = Reservation.objects.create(item=w.item, buyer=w.buyer, quantity=n, deposit_amount=0, total_price=P * n,
                                   delivery_location=w.loc, expires_at=timezone.now() + timedelta(days=1))
    with transaction.atomic():
        complete_reservation(r, 'deposit')
    return r


def stock(w):
    return Item.objects.get(pk=w.item.pk).quantity


def test_free_cod_refusal_returns_stock(w):
    co(); r = free_order(w, 3)
    assert stock(w) == 7
    o = create_outbound_delivery_order(r); o.status = 'delivered'; o.delivered_at = timezone.now(); o.save()
    x = api(w.buyer).post(f'/api/delivery-orders/{o.id}/refuse-receipt/', {}, format='json')
    ret = DeliveryOrder.objects.filter(direction='return').first()
    assert ret, x.data
    with transaction.atomic():
        execute_return_refund(ret)
    assert stock(w) == 10, stock(w)


def test_free_cod_dispute_refund_returns_stock(w):
    r = free_order(w, 3)
    Reservation.objects.filter(pk=r.pk).update(status='disputed')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller, title='t', description='d')
    resolve_dispute(d, resolution_action='refund_buyer')
    assert stock(w) == 10, stock(w)


@pytest.mark.parametrize('who', ['buyer', 'seller'])
def test_free_cod_cancel_returns_stock(w, who):
    r = free_order(w, 3)
    x = api(getattr(w, who)).post(f'/api/reservations/{r.id}/cancel/', {}, format='json')
    r.refresh_from_db()
    assert r.status != 'cancelled' or stock(w) == 10, (x.status_code, r.status, stock(w))


def test_cancel_takes_the_sold_count_back(w):
    r = free_order(w, 3)
    api(w.seller).post(f'/api/reservations/{r.id}/cancel/', {}, format='json')
    i = Item.objects.get(pk=w.item.pk)
    assert i.sold_count == 0, i.sold_count


@pytest.mark.parametrize('body', [{'title': 'مختلف'}, {'description': 'مختلف'}])
def test_what_was_sold_cannot_be_rewritten(w, body):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    before = api(w.buyer).get(f'/api/reservations/{r.id}/').data
    api(w.seller).patch(f'/api/items/{w.item.id}/', body, format='json')
    after = api(w.buyer).get(f'/api/reservations/{r.id}/').data
    a, b = before.get('data', before)['item'], after.get('data', after)['item']
    k = list(body)[0]
    assert a.get(k) == b.get(k), (a.get(k), b.get(k))


def test_reel_views_cannot_be_pumped(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    reel = Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    for _ in range(60):
        APIClient().post(f'/api/reels/{reel.id}/view/')
    reel.refresh_from_db()
    assert reel.view_count < 10, reel.view_count


def test_a_stranger_cannot_lock_the_owner_out():
    User.objects.create_user(username='v', password=PW, phone='07801234567')
    for n in range(8):
        APIClient(REMOTE_ADDR='10.0.0.%d' % n).post('/api/auth/login/', {'username': '07801234567', 'password': 'bad'}, format='json')
    x = APIClient(REMOTE_ADDR='10.9.9.9').post('/api/auth/login/', {'username': '07801234567', 'password': PW}, format='json')
    assert x.status_code == 200, x.status_code


def test_the_public_username_is_not_the_phone(w):
    x = APIClient().post('/api/auth/register/', {'first_name': 'a', 'last_name': 'b', 'phone': '07701110000', 'password': PW, 'password2': PW}, format='json')
    u = User.objects.get(phone='+9647701110000')
    Item.objects.filter(pk=w.item.pk).update(owner=u)
    body = APIClient().get(f'/api/items/{w.item.id}/').content.decode()
    assert '7701110000' not in body, u.username


def test_gone_sellers_share_pages(w):
    w.lst.is_active = True; w.lst.save()
    User.objects.filter(pk=w.seller.pk).update(is_active=False)
    x = APIClient(HTTP_HOST='testserver').get(f'/p/{w.lst.id}')
    assert x.status_code == 404, x.status_code


def test_archived_share_pages(w):
    Item.objects.filter(pk=w.item.pk).update(status='archived')
    x = APIClient(HTTP_HOST='testserver').get(f'/p/{w.lst.id}')
    assert x.status_code == 404, x.status_code


def test_card_topup_is_not_cash_out(w):
    verify(w.buyer)
    Transaction.objects.create(user=w.buyer, transaction_type='payment', amount=5000000, status='completed')
    x = withdraw(w.buyer, 5000000)
    assert x.status_code == 400, x.status_code


def test_disputable_earnings_not_laundered_by_wholesale(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full'); release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed')
    pal = user('pal'); verify(pal)
    i = Item.objects.create(owner=pal, current_holder=pal, title='x', description='d', category=w.cat, condition='new', quantity=10, status='active')
    FixedPriceListing.objects.create(item=i, price=90000, deposit_percentage=10)
    t = ItemTransfer.objects.create(item=i, from_user=pal, to_user=User.objects.get(pk=w.seller.pk), quantity=1, wholesale_price_per_unit=90000)
    act(pal, t, 'approve', {'escrow_ack': True}); act(w.seller, t, 'confirm_received')
    x = withdraw(pal, 80000)
    assert x.status_code == 400, x.status_code


def test_notice_still_due_if_badge_is_taken(w):
    b = wholesale_store(w.seller)
    act(w.seller, ask(w, 1), 'approve')
    revoke_badge(UserStore.objects.get(user=w.seller), 'wholesale', by=w.seller)
    assert needs_notice(User.objects.get(pk=w.seller.pk))


def test_pickup_stays_while_goods_are_held(w):
    from core.delivery_utils import resolve_pickup
    Location.objects.create(user=w.seller, name='wh', address_line1='a', city='b', state='basra', contact_person='p', contact_phone='07701234567', is_primary=True)
    t = approve(w, ask(w, 2, duration_days=5))
    ItemTransfer.objects.filter(pk=t.pk).update(agreement_end_date=timezone.now() - timedelta(days=1))
    loc, custodian, _ = resolve_pickup(Item.objects.get(pk=t.resulting_item_id))
    assert custodian == w.seller


def test_live_start_does_not_spam_followers(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    with patch('core.tasks.notify_store_followers_live.delay') as n:
        for _ in range(12):
            x = api(w.seller).post('/api/live-sessions/', {'title': 't', 'start_immediately': True}, format='json')
            sid = (x.data.get('data') or x.data).get('id')
            api(w.seller).post(f'/api/live-sessions/{sid}/go_live/')
            api(w.seller).post(f'/api/live-sessions/{sid}/end-session/')
    assert n.call_count <= 5, n.call_count


def test_a_seller_cannot_release_cod_without_delivery(w):
    """Accomplice confirms a free order that never shipped: no cash, but a sale."""
    r = free_order(w)
    x = api(w.buyer).post(f'/api/reservations/{r.id}/approve_and_release_payment/', {}, format='json')
    r.refresh_from_db()
    assert r.status != 'confirmed', (x.status_code, r.status, r.payment_released_to_seller)


def test_refund_goes_to_a_sellable_variant(w):
    t = approve(w, ask(w, 4))
    api(w.seller).patch(f'/api/items/{w.item.id}/', {'options': [{'name': 's', 'values': ['a']}], 'variants': [{'attributes': {'s': 'a'}, 'quantity': 6}]}, format='json')
    act(w.buyer, t, 'request_refund'); x = act(w.seller, t, 'accept_refund')
    assert (x.status_code, stock(w)) == (200, 10), (x.status_code, stock(w))


def test_banned_sellers_in_search_surfaces(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='active')
    AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                  start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    User.objects.filter(pk=w.seller.pk).update(is_active=False)
    out = {}
    for u in ('/api/auctions/', '/api/reels/', '/api/stores/', f'/api/stores/public/{s.id}/', f'/api/reels/?store={s.id}'):
        r = api(w.buyer).get(u); d = r.json()
        d = d.get('results', d.get('data', d)) if isinstance(d, dict) else d
        d = d.get('results', d) if isinstance(d, dict) else d
        out[u] = (r.status_code, len(d) if isinstance(d, list) else 'obj')
    assert all(n == 0 or c == 404 for c, n in out.values()), out


def test_banned_sellers_money_is_not_withdrawn_meanwhile(w):
    verify(w.seller)
    get_or_create_wallet(w.seller).credit(D(50000))
    x = withdraw(w.seller, 50000)
    from core.services.moderation import suspend_user
    from core.services.withdrawals import approve_withdrawal
    suspend_user(w.seller, reason='fraud', actor=w.rep)
    wd = WithdrawalRequest.objects.get()
    with pytest.raises(Exception):
        approve_withdrawal(wd, reviewed_by=w.rep)


def test_own_accounts_reviews_need_delivery(w):
    """Free COD order confirmed by an accomplice with no delivery opens 5-star reviews."""
    r = free_order(w)
    api(w.buyer).post(f'/api/reservations/{r.id}/approve_and_release_payment/', {}, format='json')
    x = api(w.buyer).post('/api/product-reviews/', {'item': w.item.id, 'score': 5, 'comment': 'x'}, format='json')
    assert x.status_code == 400, x.status_code


def test_sharing_a_location_after_delete(w):
    r = res(w)
    Reservation.objects.filter(pk=r.pk).update(delivery_location=w.loc)
    r.refresh_from_db()
    with transaction.atomic():
        complete_reservation(r, 'full')
    x = api(w.buyer).delete(f'/api/locations/{w.loc.id}/')
    r = Reservation.objects.get(pk=r.pk)
    co()
    with transaction.atomic():
        o = create_outbound_delivery_order(r)
    assert o.to_address, (x.status_code, r.delivery_location_id)


def test_held_money_survives_reseller_ban(w):
    t = approve(w, ask(w, 2))
    User.objects.filter(pk=w.buyer.pk).update(is_active=False)
    act(w.seller, t, 'claim_handover')
    from core.services.wholesale import settle_claimed_handovers
    assert settle_claimed_handovers(timezone.now() + timedelta(days=8)) == 0  # they cannot object


def test_quantity_edit_below_sold(w):
    """Rep-held pieces must survive the seller editing the stock."""
    a = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep, commission_percentage=1, status='accepted')
    api(w.buyer).post(f'/api/representatives/{a.id}/create_reservation_for_buyer/', {'item_id': w.item.id, 'quantity': 4}, format='json')
    Reservation.objects.update(expires_at=timezone.now() - timedelta(hours=1))
    api(w.seller).delete(f'/api/items/{w.item.id}/')
    from core.tasks import process_expired_reservations
    process_expired_reservations()
    i = Item.objects.get(pk=w.item.pk)
    assert i.status == 'archived', i.status


def test_spending_uses_card_money_first_and_refunds_give_it_back(w):
    from core.services.withdrawals import unspent_topups
    u = user('tp', 100000)
    Transaction.objects.create(user=u, transaction_type='payment', amount=100000, status='completed')
    assert unspent_topups(u) == 100000
    loc = Location.objects.create(user=u, name='h', address_line1='a', city='b', state='baghdad', contact_person='p', contact_phone='07701234567')
    r = Reservation.objects.create(item=w.item, buyer=u, quantity=1, deposit_amount=P / 10, total_price=P,
                                   delivery_location=loc, expires_at=timezone.now() + timedelta(days=1))
    with transaction.atomic():
        complete_reservation(r, 'full')
    assert unspent_topups(u) == 0
    api(w.seller).post(f'/api/reservations/{r.id}/cancel/', {}, format='json')
    assert unspent_topups(u) == 100000
    User.objects.filter(pk=u.pk).update(topups_withdrawable=True)
    assert unspent_topups(User.objects.get(pk=u.pk)) == 0


def test_a_bidder_still_opens_a_suspended_sellers_auction(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='active')
    a = AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    Bid.objects.create(auction=a, bidder=w.buyer, amount=2000)
    User.objects.filter(pk=w.seller.pk).update(is_active=False)
    assert api(w.buyer).get(f'/api/auctions/{a.id}/').status_code == 200
    assert APIClient().get(f'/api/auctions/{a.id}/').status_code == 404


def test_phone_usernames_are_replaced():
    import importlib
    from django.apps import apps
    m = importlib.import_module('core.migrations.0088_usernames_without_phones')
    a = User.objects.create_user(username='u9647701110000', password='x')
    b = User.objects.create_user(username='u9647701110001', password='x', is_staff=True)
    c = User.objects.create_user(username='ali_7701', password='x')
    m.rename(apps, None)
    for x in (a, b, c):
        x.refresh_from_db()
    assert a.username.startswith('user_') and (b.username, c.username) == ('u9647701110001', 'ali_7701')
