"""ما كشفه الفحص الرابع (2026-11-24): سباقات، استعلامات، صور، مبالغ كبيرة.

اختبارات السباق تفتح اتصالات حقيقية (transaction=True) — القفل لا يُختبر
بنداءين متتابعين.
"""
import io
import threading
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuctionListing, Bid, Chat, DeliveryOrder, Dispute, FixedPriceListing, Item, ItemImage,
    ItemTransfer, LiveSession, Message, Notification, PaymentIntent, ProductReview, Reel,
    Representative, Reservation, Transaction, User, UserStore,
)
from core.services.live_media import srs
from core.tasks import sync_pending_payments
from core.tests.test_abuse_holes import company
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation

pytestmark = pytest.mark.django_db


def many(w, n):
    st = getattr(w, 'st', None) or UserStore.objects.create(user=w.seller, name='s', is_active=True)
    w.st = st
    for k in range(n):
        i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t%d' % k, description='d',
                                category=w.cat, condition='new', quantity=3, status='active')
        FixedPriceListing.objects.create(item=i, price=P, deposit_percentage=10)
        ItemImage.objects.create(item=i, image='item_images/x.jpg')


def count(c, url):
    with CaptureQueriesContext(connection, ) as q:
        x = c.get(url)
    assert x.status_code == 200, (url, x.status_code)
    return len(q)


# Each load adds 8 items (4 of them listed by other sellers), so an items page
# is allowed 2 per 4 rows of each.
LIMITS = {'/api/chats/': 4, '/api/items/': 3, '/api/listings/fixed/': 3}


@pytest.mark.parametrize('url', ['/api/items/', '/api/listings/fixed/', '/api/auctions/', '/api/stores/', '/api/reels/',
                                 '/api/live-sessions/', '/api/categories/', '/api/reservations/', '/api/chats/',
                                 '/api/transactions/', '/api/notifications/', '/api/transfers/', '/api/representatives/',
                                 '/api/product-reviews/', '/api/disputes/', '/api/delivery-orders/'])
def test_query_growth(w, url):
    def load(n, tag):
        many(w, n)
        for k in range(n):
            u = User.objects.create_user(username=f'{tag}{k}', password='x')
            s = UserStore.objects.create(user=u, name='x', is_active=True)
            it = Item.objects.create(owner=u, current_holder=u, title='a', description='d', category=w.cat, condition='new', quantity=3, status='active')
            FixedPriceListing.objects.create(item=it, price=P, deposit_percentage=10)
            AuctionListing.objects.create(item=it, starting_price=1, current_price=1, minimum_increment=1, bid_deposit=0,
                                          start_time=timezone.now(), end_time=timezone.now() + timedelta(days=1), status='active')
            Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now(), item=it)
            LiveSession.objects.create(store=s, title='t', status='live', started_at=timezone.now())
            r = res(w, item=it)
            ch = Chat.objects.create(item=it, buyer=w.buyer, seller=u)
            Message.objects.create(chat=ch, sender=u, message_type='user', content='x')
            Transaction.objects.create(user=w.buyer, transaction_type='purchase', amount=1, item=it, reservation=r, status='completed')
            Notification.objects.create(user=w.buyer, title='t', body='b')
            ItemTransfer.objects.create(item=it, from_user=u, to_user=w.buyer)
            Representative.objects.create(item=it, owner=u, representative=w.buyer, commission_percentage=1)
            ProductReview.objects.create(reviewer=u, item=w.item, score=5)
            Dispute.objects.create(item=it, reservation=r, complainant=w.buyer, defendant=u, title='t', description='d', reason='other')
            DeliveryOrder.objects.create(reservation=r, direction='outbound', status='assigned')
    load(4, 'yy')
    c = api(w.buyer)
    c.get(url)
    q1 = count(c, url)
    load(4, 'zz')
    q2 = count(c, url)
    per = (q2 - q1) / 4
    # A page must not cost the database a fixed crowd of queries per row.
    assert per <= LIMITS.get(url, 2), f'{url}: {q1} -> {q2} (+{per:.1f} per row)'


def test_two_deleted_accounts(w):
    a, b = user('da'), user('db')
    x = api(a).post('/api/users/delete-account/', {'current_password': 'x'}, format='json').status_code
    y = api(b).post('/api/users/delete-account/', {'current_password': 'x'}, format='json').status_code
    assert (x, y) == (200, 200), (x, y)


FCM_URLS = ('/api/users/update-fcm-token/', '/api/users/update_fcm_token/', '/api/users/fcm-token/', '/api/users/fcm_token/')


def test_fcm_token_belongs_to_one_account(w):
    for u in (w.buyer, w.seller):
        assert any(api(User.objects.get(pk=u.pk)).post(url, {'fcm_token': 'same-device'}, format='json').status_code == 200
                   for url in FCM_URLS)
    assert list(User.objects.filter(fcm_token='same-device')) == [w.seller]


def test_expensive_goods_can_be_sold(w):
    x = api(w.seller).patch(f'/api/items/{w.item.id}/update_fixed_listing/', {'price': '60000000'}, format='json')
    assert x.status_code == 200, x.data
    c = api(w.buyer); c.raise_request_exception = False
    y = c.post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 2, 'delivery_location_id': w.loc.id}, format='json')
    assert y.status_code == 201, y.status_code
    assert Reservation.objects.get().total_price == D(120000000)


def test_price_of_a_house(w):
    c = api(w.seller); c.raise_request_exception = False
    x = c.patch(f'/api/items/{w.item.id}/update_fixed_listing/', {'price': '250000000'}, format='json')
    assert x.status_code == 200, (x.status_code, getattr(x, 'data', None))


@pytest.mark.parametrize('url', [
    '/api/items/?min_price=abc', '/api/items/?category=abc', '/api/items/?ordering=password', '/api/items/?page=0',
    '/api/listings/fixed/?min_price=abc', '/api/locations/nearby_items/?lat=abc&lng=1', '/api/locations/nearby_items/?lat=999&lng=999&radius=-1',
    '/api/locations/nearby_items/', '/api/stores/nearby/?latitude=x&longitude=y', '/api/locations/nearby_stores/?latitude=x',
    '/api/stores/public/%00/', '/api/items/abc/', '/api/reels/abc/', '/api/reels/?store=abc', '/api/auctions/abc/', '/api/product-reviews/?item=abc',
    '/api/product-reviews/my-review/abc/', '/api/wallet/activity/?limit=abc', '/api/items/?page_size=-5', '/api/items/99999999999999999999/',
    '/api/chats/abc/', '/api/ledger/dashboard/?period=custom&start=x', '/api/reservations/abc/', '/api/categories/?parent=abc',
    '/api/payments/nope/', '/api/reservations/delivery-quote/?item_id=abc', '/api/reservations/delivery-quote/'])
def test_bad_input_is_never_a_500(w, url):
    c = api(w.buyer); c.raise_request_exception = False
    UserStore.objects.get_or_create(user=w.buyer, defaults={'name': 'x', 'is_active': True})
    assert c.get(url).status_code < 500, url


@pytest.mark.parametrize('method,url,body', [
    ('post', '/api/reservations/send-to-delivery-batch/', {'reservation_ids': ['a', 'b']}),
    ('post', '/api/reservations/send-to-delivery-batch/', {'reservation_ids': [[1], [2]]}),
    ('post', '/api/stores/{s}/set_default_location/', {'location_id': 'abc'}),
    ('post', '/api/reservations/{r}/update-delivery-location/', {'delivery_location_id': 'abc'}),
    ('post', '/api/reservations/{r}/complete/', {'delivery_location_id': 'abc', 'payment_type': ['x']}),
    ('post', '/api/stores/{s}/toggle_active/', {'is_active': {}}),
    ('patch', '/api/users/{u}/', {'date_of_birth': 'x', 'phone': 'abc'}),
    ('patch', '/api/users/{u}/', {'email': 'taken@b.co'}),
    ('patch', '/api/users/{u}/', {'phone': '07709999999'}),
    ('post', '/api/live-sessions/', {'title': 'x' * 500}),
    ('post', '/api/reels/{x}/comments/', {'content': 'x' * 100000}),
    ('post', '/api/reels/{x}/comments/', {'content': {'a': 1}}),
    ('post', '/api/reports/create_report/', {'target_type': 'item', 'target_id': 'abc', 'reason': 'x'}),
    ('post', '/api/payments/webhook/alqaseh/', [1, 2]),
    ('post', '/api/payments/webhook/alqaseh/', {'order_id': {'a': 1}}),
    ('post', '/api/transfers/create_agreement/', {'item_id': 'abc'}),
    ('post', '/api/transfers/create_agreement/', {'item_id': 999999}),
    ('post', '/api/representatives/create_request/', {'item_id': 999999, 'commission_percentage': 'x'}),
    ('post', '/api/items/', {'title': 'x', 'price': 'abc', 'options': 'notjson', 'variants': '{'}),
    ('post', '/api/auth/login/', {'username': {'a': 1}, 'password': []}),
    ('post', '/api/auth/register/', {'username': 'x' * 400, 'first_name': 'a', 'last_name': 'b', 'phone': '07701239999', 'password': 'Zx9!kQ2#long', 'password2': 'Zx9!kQ2#long'}),
])
def test_bad_body_is_never_a_500(w, method, url, body):
    User.objects.filter(pk=w.seller.pk).update(email='taken@b.co', phone='+9647709999999')
    s = UserStore.objects.create(user=w.buyer, name='x', is_active=True)
    x = Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    r = res(w)
    c = api(w.buyer); c.raise_request_exception = False
    y = getattr(c, method)(url.format(s=s.id, r=r.id, u=w.buyer.id, x=x.id), body, format='json')
    assert y.status_code < 500, (url, y.status_code)


def test_reel_comment_length(w):
    s = UserStore.objects.create(user=w.seller, name='x', is_active=True)
    x = Reel.objects.create(store=s, status='published', video_key='v', thumbnail_key='t', duration_seconds=1, published_at=timezone.now())
    y = api(w.buyer).post(f'/api/reels/{x.id}/comments/', {'content': 'x' * 100000}, format='json')
    assert y.status_code == 400, y.status_code


def big_png():
    from PIL import Image
    b = io.BytesIO()
    import os
    Image.frombytes('RGB', (3000, 3000), os.urandom(27_000_000)).save(b, 'PNG')
    return b.getvalue()


def test_image_size_is_capped(w):
    data = big_png()
    f = SimpleUploadedFile('a.png', data, content_type='image/png')
    x = api(w.seller).post(f'/api/items/{w.item.id}/upload_images/', {'images': f})
    assert x.status_code == 400 and not ItemImage.objects.exists()


def test_item_image_must_be_an_image(w):
    f = SimpleUploadedFile('a.svg', b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', content_type='image/png')
    api(w.seller).post(f'/api/items/{w.item.id}/upload_images/', {'images': f})
    assert not ItemImage.objects.filter(item=w.item).exists(), ItemImage.objects.filter(item=w.item).first().image.name


def small(fmt, name):
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (8, 8)).save(b, fmt)
    return SimpleUploadedFile(name, b.getvalue(), content_type='text/html')


@pytest.mark.parametrize('fmt,ext', [('PNG', 'png'), ('JPEG', 'jpg'), ('WEBP', 'webp')])
def test_a_real_picture_is_stored_as_what_it_is(w, fmt, ext):
    x = api(w.seller).post(f'/api/items/{w.item.id}/upload_images/', {'images': small(fmt, 'page.html')})
    assert x.status_code == 201, x.data
    assert ItemImage.objects.get().image.name.endswith('.' + ext)


def test_profile_and_store_pictures_are_checked_too(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    bad = lambda: SimpleUploadedFile('a.svg', b'<svg><script>1</script></svg>', content_type='image/png')  # noqa: E731
    assert api(w.seller).patch(f'/api/users/{w.seller.id}/', {'avatar': bad()}).status_code == 400
    assert api(w.seller).post(f'/api/stores/{st.id}/update_logo/', {'logo': bad()}).status_code == 400
    assert api(w.seller).post(f'/api/stores/{st.id}/update_logo/', {'logo': small('PNG', 'l.png')}).status_code == 200


def intent(u, **k):
    made = k.pop('made', timezone.now() - timedelta(hours=60))
    x = PaymentIntent.objects.create(user=u, provider='alqaseh', purpose=PaymentIntent.PURPOSE_WALLET_TOPUP, amount=D(5000),
                                     currency='IQD', reference='R%s' % PaymentIntent.objects.count(), provider_payment_id='p', **k)
    PaymentIntent.objects.filter(pk=x.pk).update(created_at=made)
    return x


def test_a_paid_top_up_nobody_reported_is_settled(w, monkeypatch):
    from core.services.payments import service
    from core.services.payments.base import PaymentStatus

    class Gateway:
        def fetch_status(self, *, intent):
            return PaymentStatus(state='succeeded', raw={})
    monkeypatch.setattr(service, 'get_provider', lambda name: Gateway())
    before = bal(w.buyer)
    intent(w.buyer)                                                     # past the app's 48 hours
    intent(w.buyer, made=timezone.now())                                # still being paid
    intent(w.buyer, made=timezone.now() - timedelta(days=30))           # given up on
    for _ in range(2):
        sync_pending_payments()
    assert bal(w.buyer) == before + 5000


@pytest.fixture
def box(monkeypatch, settings):
    """A media server with three connections on the stream and one elsewhere."""
    import requests
    settings.SRS_API_ORIGIN = 'http://box:1985'
    settings.LIVE_MEDIA_PROVIDER = 'srs'
    cut = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {'clients': box.clients}
    monkeypatch.setattr(requests, 'get', lambda *a, **k: R())
    monkeypatch.setattr(requests, 'delete', lambda url, **k: cut.append(url.rsplit('/', 1)[-1]))
    box.clients = []
    return cut


def on_air(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    ses = LiveSession.objects.create(store=st, title='t', status='live', started_at=timezone.now())
    box.clients = [{'id': 'pub', 'name': ses.stream_name}, {'id': 'v1', 'name': ses.stream_name},
                   {'id': 'v2', 'url': '/live/' + ses.stream_name}, {'id': 'other', 'name': 'live_x'}]
    srs.remember_client('v1', w.buyer.id)
    srs.remember_client('v2', w.buyer2.id)
    return ses


def test_a_ban_cuts_the_picture_already_playing(w, box, django_capture_on_commit_callbacks):
    ses = on_air(w)
    with django_capture_on_commit_callbacks(execute=True):
        x = api(w.seller).post(f'/api/live-sessions/{ses.id}/bans/', {'user_id': w.buyer.id, 'reason': 'x'}, format='json')
    assert (x.status_code, box) == (201, ['v1'])


def test_ending_a_stream_stops_everyone_on_it(w, box, django_capture_on_commit_callbacks):
    ses = on_air(w)
    with django_capture_on_commit_callbacks(execute=True):
        api(w.seller).post(f'/api/live-sessions/{ses.id}/end-session/')
    assert sorted(box) == ['pub', 'v1', 'v2']


def test_an_unreachable_box_does_not_stop_the_end(w, box, monkeypatch, django_capture_on_commit_callbacks):
    import requests

    def down(*a, **k):
        raise requests.ConnectionError('down')
    ses = on_air(w)
    monkeypatch.setattr(requests, 'get', down)
    with django_capture_on_commit_callbacks(execute=True):
        assert api(w.seller).post(f'/api/live-sessions/{ses.id}/end-session/').status_code == 200
    ses.refresh_from_db()
    assert ses.status == 'ended'


def test_the_play_hook_remembers_whose_connection_it_is(w, settings):
    from django.core.cache import cache
    settings.SRS_HOOK_SECRET = 's'
    settings.LIVE_TOKEN_SECRET = 'k'
    ses = on_air(w)
    token = srs.make_stream_token(ses.stream_name, srs.ROLE_SUBSCRIBER, 60, user_id=w.buyer.id)
    x = APIClient().post('/api/live/srs/on_play/?secret=s', {'stream': ses.stream_name, 'param': '?token=' + token,
                                                            'client_id': 'abc'}, format='json')
    assert (x.data['code'], cache.get(srs.client_key('abc'))) == (0, w.buyer.id)


@pytest.mark.django_db(transaction=True)
def test_send_to_delivery_once(w):
    company(w)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    codes, gate = [], threading.Barrier(4)

    def go():
        c = api(User.objects.get(pk=w.seller.pk))
        gate.wait()
        codes.append(c.post(f'/api/reservations/{r.id}/send-to-delivery/').status_code)
        connections.close_all()
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    n = DeliveryOrder.objects.filter(reservation=r, direction='outbound').count()
    assert n == 1, (n, codes)


@pytest.mark.django_db(transaction=True)
def test_dispute_once(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    codes, gate = [], threading.Barrier(4)

    def go():
        c = api(User.objects.get(pk=w.buyer.pk))
        gate.wait()
        codes.append(c.post('/api/disputes/create_dispute/', {'item_id': w.item.id, 'reservation_id': r.id, 'title': 't', 'description': 'd', 'reason': 'other'}, format='json').status_code)
        connections.close_all()
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert Dispute.objects.filter(reservation=r).count() == 1, (Dispute.objects.count(), codes)


@pytest.mark.django_db(transaction=True)
def test_release_race_pays_once(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    before, gate, codes = total(), threading.Barrier(4), []

    def go():
        c = api(User.objects.get(pk=w.buyer.pk))
        gate.wait()
        codes.append(c.post(f'/api/reservations/{r.id}/approve_and_release_payment/').status_code)
        connections.close_all()
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert total() == before + P, (total() - before, codes)


@pytest.mark.django_db(transaction=True)
def test_cancel_vs_pay_race(w):
    r = res(w)
    before, gate, out = total(), threading.Barrier(2), {}

    def go(k, url, body):
        c = api(User.objects.get(pk=w.buyer.pk))
        gate.wait()
        out[k] = c.post(url, body, format='json').status_code
        connections.close_all()
    ts = [threading.Thread(target=go, args=a) for a in (
        ('pay', f'/api/reservations/{r.id}/complete/', {'payment_type': 'full'}),
        ('cancel', f'/api/reservations/{r.id}/cancel/', {}))]
    [t.start() for t in ts]; [t.join() for t in ts]
    r.refresh_from_db()
    w.item.refresh_from_db()
    spent = before - total()
    assert (r.status == 'completed' and spent == P and w.item.quantity == 9) or (
        r.status == 'cancelled' and spent == 0 and w.item.quantity == 10), (out, r.status, spent, w.item.quantity)


@pytest.mark.django_db(transaction=True)
def test_wholesale_reject_vs_approve_race(w):
    t = ItemTransfer.objects.create(item=w.item, from_user=w.seller, to_user=w.buyer, quantity=1, wholesale_price_per_unit=P)
    gate, out = threading.Barrier(2), {}

    def go(k, who):
        c = api(User.objects.get(pk=who.pk))
        gate.wait()
        out[k] = c.post(f'/api/transfers/{t.id}/{k}/', {}, format='json').status_code
        connections.close_all()
    ts = [threading.Thread(target=go, args=a) for a in (('approve', w.seller), ('cancel', w.buyer))]
    [x.start() for x in ts]; [x.join() for x in ts]
    t.refresh_from_db()
    paid = bal(w.buyer) != D(5000000)
    assert paid == (t.status == 'active'), (out, t.status, paid)


@pytest.mark.django_db(transaction=True)
def test_bids_race_keeps_one_winner(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=100,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    gate, out = threading.Barrier(4), []

    def go(n):
        c = api(User.objects.get(pk=(w.buyer, w.buyer2)[n % 2].pk))
        gate.wait()
        out.append(c.post(f'/api/auctions/{a.id}/place_bid/', {'amount': 2000 + n}, format='json').status_code)
        connections.close_all()
    ts = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert Bid.objects.filter(auction=a, is_winning=True).count() == 1, out
    spent = D(10000000) - bal(w.buyer) - bal(w.buyer2)
    assert spent == 100 * Bid.objects.values('bidder').distinct().count(), (spent, out)
