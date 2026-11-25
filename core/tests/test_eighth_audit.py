"""ما كشفه الفحص الثامن (2026-11-26)."""
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch
import pytest
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient
from core.models import *
from core.models import get_or_create_wallet
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.tests.test_wholesale_money import act, approve, ask, sell, verify, withdraw
from core.wallet_utils import complete_reservation, release_reservation_payment
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear(); yield; cache.clear()


def test_message_push_payload_is_one_fcm_accepts(w):
    from core.notifications import notify_new_message
    ch = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=ch, sender=w.buyer, message_type='user', content='م' * 3900)
    with patch('core.notifications.notify_push') as n:
        notify_new_message(m)
    d = n.call_args.kwargs['data']
    assert 'message_type' not in d and 'from' not in d, list(d)
    assert len(str(d).encode()) < 3500, len(str(d).encode())


def live(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    return s, LiveSession.objects.create(store=s, title='t', status='live', started_at=timezone.now())


@pytest.mark.parametrize('q', ['abc', '-5', '0'])
def test_comment_limits(w, q):
    s, l = live(w)
    r = Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    c = APIClient(); c.raise_request_exception = False
    codes = [c.get(f'/api/live-sessions/{l.id}/comments/?limit={q}').status_code, c.get(f'/api/reels/{r.id}/comments/?limit={q}').status_code]
    assert max(codes) < 500, codes


@pytest.mark.parametrize('u', ['/api/ledger/entries/?contact=abc', '/api/ledger/entries/?contact=99999999999999999999', '/api/ledger/contacts/abc/', '/api/reservations/delivery-quote/?item_id=abc&location_id=x',
                               '/api/listings/fixed/1/similar/?limit=-5', '/api/messages/?chat=abc', '/api/transactions/?user=abc', '/api/ratings/?rated_user=abc'])
def test_odd_queries(w, u):
    UserStore.objects.get_or_create(user=w.buyer, defaults={'name': 'x', 'is_active': True})
    c = api(w.buyer); c.raise_request_exception = False
    assert c.get(u).status_code < 500


def test_contact_cannot_look_up_people_by_id(w):
    UserStore.objects.create(user=w.buyer, name='x', is_active=True)
    x = api(w.buyer).post('/api/ledger/contacts/', {'kind': 'customer', 'name': 'a', 'linked_user': w.rep.id}, format='json')
    assert x.data.get('linked_user_username') is None, x.data.get('linked_user_username')


def test_anonymous_flood_is_slowed(w, settings):
    settings.REQUEST_RATES = {**settings.REQUEST_RATES, 'visitor_device': '300/min'}
    c = APIClient()
    codes = [c.get('/api/listings/fixed/').status_code for _ in range(400)]
    assert 429 in codes, set(codes)


def test_payment_opening_is_limited(w):
    with patch('core.payment_views.create_intent', side_effect=lambda **k: PaymentIntent.objects.create(user=w.buyer, provider='alqaseh', amount=1000, reference='r%d' % PaymentIntent.objects.count())) as n:
        for _ in range(60):
            api(w.buyer).post('/api/payments/create/', {'provider': 'alqaseh', 'amount': '1000'}, format='json')
    assert n.call_count < 40, n.call_count


@pytest.mark.parametrize('days', [2000000000, 100000])
def test_warranty_is_bounded(w, days):
    c = api(w.seller); c.raise_request_exception = False
    x = c.patch(f'/api/items/{w.item.id}/', {'warranty_days': days}, format='json')
    assert x.status_code == 400, (x.status_code, Item.objects.get(pk=w.item.pk).warranty_days)


def test_one_bad_order_does_not_stop_the_release_of_others(w):
    from core.tasks import release_payments_after_week
    co = DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    rs = []
    for d in (2000000000, 1):
        r = res(w)
        with transaction.atomic():
            complete_reservation(r, 'full')
        Reservation.objects.filter(pk=r.pk).update(warranty_days=d)
        DeliveryOrder.objects.create(reservation=r, company=co, direction='outbound', status='delivered', delivered_at=timezone.now() - timedelta(days=30))
        rs.append(r)
    release_payments_after_week()
    rs[1].refresh_from_db()
    assert rs[1].payment_released_to_seller


def test_listing_deposit_bounds(w):
    c = api(w.seller); c.raise_request_exception = False
    for v in ('500', '-5', 'NaN'):
        x = c.patch(f'/api/items/{w.item.id}/update_fixed_listing/', {'deposit_percentage': v}, format='json')
        assert x.status_code == 400, (v, x.status_code)


@pytest.mark.parametrize('lat,lng', [('NaN', '1'), ('999', '1'), ('1e400', '1')])
def test_locations_are_real(w, lat, lng):
    c = api(w.buyer); c.raise_request_exception = False
    x = c.patch(f'/api/locations/{w.loc.id}/', {'latitude': lat, 'longitude': lng}, format='json')
    assert x.status_code == 400, x.status_code


def test_auction_dates_are_bounded(w):
    c = api(w.seller); c.raise_request_exception = False
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='draft')
    x = c.post(f'/api/items/{i.id}/create_auction/', {'starting_price': '1000', 'minimum_increment': '100', 'bid_deposit': '50', 'start_time': timezone.now().isoformat(), 'end_time': (timezone.now() + timedelta(days=3650)).isoformat()}, format='json')
    assert x.status_code == 400, x.status_code


def test_old_access_token_dies_with_the_password(w):
    from datetime import datetime
    from rest_framework_simplejwt.tokens import RefreshToken
    from core.channels_auth import _get_user_from_token
    u = User.objects.create_user(username='zz', password='Zx9!kQ2#long')
    old = RefreshToken.for_user(u).access_token
    old.payload['iat'] -= 60
    c = APIClient(); c.credentials(HTTP_AUTHORIZATION='Bearer %s' % old)
    assert c.get('/api/users/me/').status_code == 200
    x = c.post('/api/auth/change-password/', {'old_password': 'Zx9!kQ2#long', 'new_password': 'Other9!pass#', 'new_password2': 'Other9!pass#'}, format='json')
    assert x.status_code == 200, x.data
    assert c.get('/api/users/me/').status_code == 401
    u.refresh_from_db()
    fresh = RefreshToken.for_user(u).access_token
    c.credentials(HTTP_AUTHORIZATION='Bearer %s' % fresh)
    assert c.get('/api/users/me/').status_code == 200



def test_old_notifications_and_tokens_are_cleaned():
    from django.conf import settings
    names = ' '.join(v['task'] for v in settings.CELERY_BEAT_SCHEDULE.values())
    assert 'notification' in names and ('flush' in names or 'token' in names), names


def test_bid_on_a_removed_item(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='archived')
    a = AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    x = api(w.buyer).post(f'/api/auctions/{a.id}/place_bid/', {'amount': '2000'}, format='json')
    assert x.status_code == 400, x.status_code


def test_wholesale_request_to_a_banned_rep_or_self_chain(w):
    """A seller cannot buy wholesale from himself through his own copy."""
    t = approve(w, ask(w, 2))
    FixedPriceListing.objects.create(item=t.resulting_item, price=P, deposit_percentage=10)
    Item.objects.filter(pk=t.resulting_item_id).update(status='active')
    before = total() + sum(x.held_amount for x in ItemTransfer.objects.all())
    get_or_create_wallet(w.seller).credit(D(1000000)); before += 1000000
    x = ItemTransfer.objects.create(item=Item.objects.get(pk=t.resulting_item_id), from_user=w.buyer, to_user=User.objects.get(pk=w.seller.pk), quantity=2, wholesale_price_per_unit=P)
    act(w.buyer, x, 'approve', {'escrow_ack': True}); act(w.seller, x, 'confirm_received')
    t.refresh_from_db(); x.refresh_from_db()
    assert (t.held_quantity, x.held_quantity) == (0, 0)
    assert total() + sum(y.held_amount for y in ItemTransfer.objects.all()) == before


def test_store_closed_goods_not_for_sale(w):
    UserStore.objects.create(user=w.seller, name='s', is_active=False)
    x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 400, x.status_code


def test_mass_notification_preview(w):
    from core.notifications import notify_push
    from core import notifications
    with patch.object(notifications, 'logger'):
        notify_push(w.buyer, 't' * 5000, 'b' * 20000, data={'type': 'general'})
    assert Notification.objects.filter(user=w.buyer).exists()


def test_a_push_never_carries_a_reserved_key():
    from core.firebase import MAX_DATA_BYTES, fcm_data
    d = fcm_data({'from': 'x', 'message_type': 'x', 'google.a': 1, 'gcm_x': 1, 'type': 't', 'content': 'م' * 9000})
    assert set(d) == {'type', 'content'} and sum(len(k) + len(v.encode()) for k, v in d.items()) <= MAX_DATA_BYTES


def test_closing_the_shop_hides_its_goods_and_finishes_its_orders(w):
    r = res(w)
    s = UserStore.objects.create(user=w.seller, name='s', is_active=False)
    assert APIClient().get('/api/listings/fixed/').json()['results'] == []
    x = api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'full', 'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 200, x.data
    s.is_active = True; s.save()
    assert len(APIClient().get('/api/listings/fixed/').json()['results']) == 1


def test_members_and_addresses_have_their_own_ceilings(w, settings):
    settings.REQUEST_RATES = {**settings.REQUEST_RATES, 'member': '5/min', 'visitor_address': '8/min'}
    codes = [api(w.buyer).get('/api/wallet/me/').status_code for _ in range(7)]
    assert codes.count(429) == 2
    codes = [APIClient(HTTP_USER_AGENT='ua%d' % n).get('/api/listings/fixed/').status_code for n in range(10)]
    assert codes.count(429) == 2


def test_the_ceiling_opens_when_the_cache_is_down(w):
    with patch('core.throttling.SimpleRateThrottle.allow_request', side_effect=ConnectionError):
        assert APIClient().get('/api/listings/fixed/').status_code == 200


def test_old_notifications_go(w):
    from core.tasks import cleanup_old_notifications_and_tokens
    a, b = (Notification.objects.create(user=w.buyer, notification_type='general', title='t', body='b') for _ in range(2))
    Notification.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(days=91))
    cleanup_old_notifications_and_tokens()
    assert list(Notification.objects.values_list('pk', flat=True)) == [b.pk]


def test_a_month_long_auction_is_fine(w):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='draft')
    x = api(w.seller).post(f'/api/items/{i.id}/create_auction/', {'starting_price': '1000', 'minimum_increment': '100', 'bid_deposit': '50', 'start_time': (timezone.now() + timedelta(minutes=5)).isoformat(), 'end_time': (timezone.now() + timedelta(days=29)).isoformat()}, format='json')
    assert x.status_code == 201, x.data
