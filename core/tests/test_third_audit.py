"""مشاكل كشفها الفحص الثالث (2026-11-24)، كلّ اختبار يثبّت السلوك الصحيح."""
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.delivery_utils import create_outbound_delivery_order
from core.models import (
    AuctionListing, Bid, Chat, DeliveryOrder, Dispute, Item, LiveSession, LiveSessionItem,
    LiveSessionModerator, Message, Reel, Reservation, User, UserStore,
)
from core.services.moderation import archive_item
from core.live_utils import end_live_session
from core.tests.test_abuse_holes import company
from core.tests.test_live_shopping import make_screenshot
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.services.disputes import resolve_dispute
from core.wallet_utils import complete_reservation

pytestmark = pytest.mark.django_db

@pytest.fixture
def a_courier(db):
    """A deposit or a live order is finished by a courier collecting the rest;
    without one it is refused (twelfth audit). Fees 0: the sums here are the
    goods'."""
    from core.models import DeliveryCompany, User
    DeliveryCompany.objects.create(
        name='courier', account=User.objects.create_user(username='courier_acc', password='x'),
        is_active=True, intra_governorate_fee=0, inter_governorate_fee=0)



@pytest.fixture
def live(w):
    w.store = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    w.s = LiveSession.objects.create(store=w.store, title='t', status='live', started_at=timezone.now())
    w.q = LiveSessionItem.objects.create(session=w.s, item=w.item, price=P, deposit_amount=D(10000), status='now_showing')
    return w


def reserve(w, **k):
    return api(w.buyer).post(f'/api/live-sessions/{w.s.id}/reserve/', {
        'queue_item_id': w.q.id, 'quantity': 1, 'screenshot': make_screenshot(), **k})


@pytest.mark.parametrize('who', ['anon', 'buyer'])
def test_strangers_cannot_edit_a_live_session(live, who):
    c = APIClient() if who == 'anon' else api(live.buyer)
    x = c.patch(f'/api/live-sessions/{live.s.id}/', {'title': 'HACKED', 'status': 'ended', 'description': 'x'}, format='json')
    live.s.refresh_from_db()
    assert (live.s.title, live.s.status) == ('t', 'live'), (x.status_code, live.s.title, live.s.status)


def test_reservation_patch_changes_nothing(w):
    r = res(w)
    before = {f.attname: getattr(Reservation.objects.get(pk=r.pk), f.attname) for f in Reservation._meta.fields}
    for u in (w.buyer, w.seller):
        for body in ({'delivery_fee': '0'}, {'warranty_days': 999}, {'platform_commission_amount': '0'},
                     {'platform_commission_percent': '0'}, {'coupon_discount': '9'}, {'is_transfer_resale': True},
                     {'representative': w.rep.id}, {'commission_amount': '5'}, {'total_price': '1'},
                     {'deposit_amount': '0'}, {'expires_at': '2030-01-01T00:00:00Z'}, {'reserved_at': '2030-01-01T00:00:00Z'}):
            api(u).patch(f'/api/reservations/{r.id}/', body, format='json')
    after = {f.attname: getattr(Reservation.objects.get(pk=r.pk), f.attname) for f in Reservation._meta.fields}
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not diff, diff


def test_reservation_plain_post_refused(w):
    x = api(w.buyer).post('/api/reservations/', {'item': w.item.id, 'quantity': 1}, format='json')
    assert x.status_code in (404, 405) and not Reservation.objects.exists(), x.status_code


def test_live_sale_charges_delivery(live):
    c = company(live)
    x = reserve(live, delivery_location_id=live.loc.id)
    assert x.status_code == 201, x.data
    o = create_outbound_delivery_order(Reservation.objects.get())
    assert o.delivery_fee > 0 and o.cash_to_collect == D(90000) + o.delivery_fee, (o.delivery_fee, o.cash_to_collect)


def test_live_cannot_sell_an_archived_item(live):
    archive_item(Item.objects.get(pk=live.item.pk), reason='x', actor=live.rep)
    x = reserve(live, delivery_location_id=live.loc.id)
    assert x.status_code == 400, x.status_code


def test_live_ignores_deleted_addresses(live):
    live.loc.is_active = False; live.loc.save()
    x = reserve(live)
    assert x.status_code == 400, x.status_code


def test_deleted_address_cannot_be_used(w):
    w.loc.is_active = False; w.loc.save()
    x = api(w.buyer).post('/api/reservations/create_reservation/', {
        'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id}, format='json')
    assert not Reservation.objects.filter(delivery_location=w.loc).exists(), x.status_code


def show(w, who=None, **k):
    return api(who or w.seller).post(f'/api/live-sessions/{w.s.id}/show-item/', {'item_id': w.item.id, **k})


@pytest.mark.usefixtures('a_courier')
def test_live_price_cannot_jump_under_the_tap(live):
    LiveSessionItem.objects.filter(pk=live.q.pk).update(shown_at=timezone.now())
    assert show(live, price='900000', deposit_amount='900000').status_code == 200
    assert reserve(live, delivery_location_id=live.loc.id).status_code == 400
    assert bal(live.buyer) == D(5000000)
    live.q = LiveSessionItem.objects.get(status='now_showing')
    assert live.q.price == D(900000)
    assert reserve(live, delivery_location_id=live.loc.id).status_code == 201


def test_showing_again_at_the_same_price_keeps_the_entry(live):
    LiveSessionItem.objects.filter(pk=live.q.pk).update(shown_at=timezone.now())
    show(live, price='100000', deposit_amount='10000')
    assert LiveSessionItem.objects.get().status == 'now_showing'


def test_queue_order_text_is_not_a_500(live):
    live.q.status = 'queued'; live.q.save()
    c = api(live.seller); c.raise_request_exception = False
    x = c.patch(f'/api/live-sessions/{live.s.id}/queue-items/{live.q.id}/', {'order': 'abc'}, format='json')
    assert x.status_code < 500, x.status_code


def test_quick_live_item_can_be_listed_after_the_stream(live):
    x = api(live.seller).post(f'/api/live-sessions/{live.s.id}/quick-show/', {
        'title': 'new', 'price': '5000', 'deposit_amount': '0', 'quantity': 3}, format='json')
    assert x.status_code == 201, x.data
    i = Item.objects.get(title='new')
    with transaction.atomic():
        end_live_session(LiveSession.objects.get(pk=live.s.pk))
    y = api(live.seller).post(f'/api/items/{i.id}/create_fixed_listing/', {'price': '5000', 'deposit_percentage': '0'}, format='json')
    i.refresh_from_db()
    assert y.status_code == 201, (y.status_code, y.data, i.status)


@pytest.mark.usefixtures('a_courier')
def test_live_sale_is_in_the_sellers_book(live):
    from ledger.models import LedgerEntry
    reserve(live, delivery_location_id=live.loc.id)
    assert LedgerEntry.objects.filter(store=live.store).exists()


def test_moderator_cannot_sell_at_any_price(live):
    LiveSessionModerator.objects.create(session=live.s, user=live.buyer2, added_by=live.seller)
    x = api(live.buyer2).post(f'/api/live-sessions/{live.s.id}/show-item/', {
        'item_id': live.item.id, 'price': '1', 'deposit_amount': '0.5'}, format='json')
    live.q.refresh_from_db()
    assert (x.status_code, live.q.price) == (403, P)
    assert show(live, who=live.buyer2).status_code == 200


def test_the_owner_edits_the_title_but_not_the_state(live):
    x = api(live.seller).patch(f'/api/live-sessions/{live.s.id}/', {
        'title': 'new', 'status': 'ended', 'total_views': 9999}, format='json')
    live.s.refresh_from_db()
    assert (x.status_code, live.s.title, live.s.status, live.s.total_views) == (200, 'new', 'live', 0)


def test_another_store_cannot_edit_it(live):
    UserStore.objects.create(user=live.buyer, name='b', is_active=True)
    x = api(live.buyer).patch(f'/api/live-sessions/{live.s.id}/', {'title': 'x'}, format='json')
    assert x.status_code == 403


def test_a_listed_item_stays_published_after_the_stream(live):
    with transaction.atomic():
        end_live_session(LiveSession.objects.get(pk=live.s.pk))
    live.item.refresh_from_db()
    assert live.item.status == 'active'


@pytest.mark.usefixtures('a_courier')
def test_a_live_order_called_off_after_the_stream_returns_to_draft(live):
    x = api(live.seller).post(f'/api/live-sessions/{live.s.id}/quick-show/', {
        'title': 'new', 'price': '5000', 'deposit_amount': '1000', 'quantity': 1}, format='json')
    live.q = LiveSessionItem.objects.get(item__title='new')
    assert reserve(live, delivery_location_id=live.loc.id).status_code == 201
    i = Item.objects.get(title='new')
    assert i.status == 'sold'
    with transaction.atomic():
        end_live_session(LiveSession.objects.get(pk=live.s.pk))
    r = Reservation.objects.get()
    assert api(live.buyer).post(f'/api/reservations/{r.id}/cancel/').status_code == 200
    i.refresh_from_db()
    assert (i.status, i.quantity) == ('draft', 1)


def test_show_item_negative_price(live):
    x = api(live.seller).post(f'/api/live-sessions/{live.s.id}/show-item/', {'item_id': live.item.id, 'price': '-5', 'deposit_amount': '1'}, format='json')
    live.q.refresh_from_db()
    assert live.q.price > 0, live.q.price


def test_show_item_deposit_above_price(live):
    x = api(live.seller).post(f'/api/live-sessions/{live.s.id}/show-item/', {'item_id': live.item.id, 'price': '1000', 'deposit_amount': '50000'}, format='json')
    live.q.refresh_from_db()
    assert live.q.deposit_amount <= live.q.price, (x.status_code, live.q.price, live.q.deposit_amount)


def carried(w, kind='full'):
    c = company(w)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, kind)
    o = create_outbound_delivery_order(r)
    o.status = 'picked_up'
    o.save()
    return c, r, o


def fail(c, o, reason='الزبون غائب'):
    return api(c.account).post(f'/api/delivery-orders/{o.id}/mark-failed/', {'reason': reason}, format='json')


@pytest.mark.parametrize('kind', ['full', 'deposit'])
def test_courier_reports_a_failed_delivery_and_an_admin_settles_it(w, kind):
    before = total()
    c, r, o = carried(w, kind)
    assert fail(c, o, '').status_code == 400
    assert fail(c, o).status_code == 200
    o.refresh_from_db()
    r.refresh_from_db()
    back = DeliveryOrder.objects.get(direction='return')
    assert (o.status, r.status, back.status) == ('failed', 'disputed', 'picked_up')
    resolve_dispute(Dispute.objects.get(), resolution_action='refund_buyer')
    assert total() == before and bal(w.buyer) == D(5000000)
    w.item.refresh_from_db()
    assert w.item.quantity == 10
    assert fail(c, o).status_code == 400


def test_only_the_assigned_company_reports_failure(w):
    c, r, o = carried(w)
    assert api(w.buyer).post(f'/api/delivery-orders/{o.id}/mark-failed/', {'reason': 'x'}).status_code in (403, 404)


def test_return_leg_can_be_closed(w):
    c = company(w)
    r = res(w)
    o = DeliveryOrder.objects.create(reservation=r, direction='return', company=c, status='picked_up')
    x = api(c.account).post(f'/api/delivery-orders/{o.id}/mark-arrived/')
    o.refresh_from_db()
    assert o.status == 'delivered', x.status_code


def test_portal_sees_every_order(w):
    c = company(w)
    r = res(w)
    for _ in range(25):
        DeliveryOrder.objects.create(reservation=r, direction='outbound', company=c, status='assigned')
    x = api(c.account).get('/api/delivery-orders/?status=assigned&page_size=100')
    assert len(x.data['results']) == 25, len(x.data['results'])


def test_store_patch_cannot_raise_level(live):
    f = [x.name for x in UserStore._meta.fields]
    before = {k: getattr(UserStore.objects.get(pk=live.store.pk), k) for k in f}
    for k in ('level', 'rating', 'total_sales', 'is_verified', 'followers_count'):
        if k in f:
            api(live.seller).patch(f'/api/stores/{live.store.id}/', {k: 999}, format='json')
    after = {k: getattr(UserStore.objects.get(pk=live.store.pk), k) for k in f}
    assert {k for k in f if before[k] != after[k] and k != 'updated_at'} == set()


def test_user_patch_cannot_raise_trust(w):
    for b in ({'trust_score': 100}, {'total_sales': 999}, {'is_staff': True}, {'is_superuser': True}, {'phone_verified': True},
              {'identity_verified': True}, {'date_joined': '2001-01-01T00:00:00Z'}):
        api(w.buyer).patch(f'/api/users/{w.buyer.id}/', b, format='json')
    u = User.objects.get(pk=w.buyer.pk)
    assert (u.trust_score, u.total_sales, u.is_staff, u.is_superuser, u.phone_verified, u.identity_verified) == (0, 0, False, False, False, False)
    assert u.date_joined.year > 2001, u.date_joined


def test_unpublished_reel_is_private(live):
    r = Reel.objects.create(store=live.store, status='archived', video_key='v', thumbnail_key='t', duration_seconds=1)
    x = APIClient().get(f'/api/reels/{r.id}/')
    assert x.status_code == 404, x.status_code


def test_draft_and_archived_items_are_private(w):
    out = {}
    for st in ('draft', 'archived'):
        Item.objects.filter(pk=w.item.pk).update(status=st)
        out[st] = APIClient().get(f'/api/items/{w.item.id}/').status_code
        ids = [i['id'] for i in (APIClient().get('/api/items/').data.get('results') or [])]
        out[st + '_list'] = w.item.id in ids
    assert out == {'draft': 404, 'draft_list': False, 'archived': 404, 'archived_list': False}, out


def test_chat_message_length(w):
    chat = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    x = api(w.buyer).post(f'/api/chats/{chat.id}/send_message/', {'content': 'x' * 200000}, format='json')
    assert x.status_code == 400, x.status_code


def test_chat_attachment_type(w):
    from django.core.files.uploadedfile import SimpleUploadedFile
    chat = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    f = SimpleUploadedFile('a.html', b'<script>alert(1)</script>', content_type='text/html')
    x = api(w.buyer).post(f'/api/chats/{chat.id}/send_message/', {'attachment': f})
    assert x.status_code == 400, x.status_code


def test_public_item_does_not_show_sellers_money(w):
    body = json.dumps(APIClient().get(f'/api/items/{w.item.id}/').json())
    hits = [k for k in ('cost_price', 'contact_phone', 'contact_person', 'address_line1', 'email') if f'"{k}"' in body]
    w.item.current_location = w.loc; w.item.save()
    body = json.dumps(APIClient().get(f'/api/items/{w.item.id}/').json())
    hits += [k for k in ('cost_price', 'contact_phone', 'contact_person', 'address_line1', 'email') if f'"{k}"' in body]
    assert not hits, hits


def test_auction_bidders_are_not_exposed(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    User.objects.filter(pk=w.buyer.pk).update(phone='+9647801234567', email='b@b.co')
    Bid.objects.create(auction=a, bidder=w.buyer, amount=5000, is_winning=True)
    body = json.dumps(APIClient().get(f'/api/auctions/{a.id}/').json())
    assert '7801234567' not in body and 'b@b.co' not in body


def test_seller_net_only_for_seller(w):
    r = res(w)
    b = api(w.buyer).get(f'/api/reservations/{r.id}/').json()
    b = b.get('data', b)
    assert b.get('seller_net') is None, b.get('seller_net')
    b = api(w.seller).get(f'/api/reservations/{r.id}/').json()
    assert b.get('data', b)['seller_net'] == '97000.00'


def test_auction_owner_cannot_lower_hidden_start_after_listing(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    x = api(w.seller).patch(f'/api/items/{w.item.id}/update_auction/{a.id}/', {'starting_price': '9000'}, format='json')
    a.refresh_from_db()
    assert x.status_code != 200 or a.current_price == a.starting_price, (a.starting_price, a.current_price)


def test_editing_auction_end_to_past(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=0,
                                      start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')
    x = api(w.seller).patch(f'/api/items/{w.item.id}/update_auction/{a.id}/', {'end_time': (timezone.now() - timedelta(hours=1)).isoformat()}, format='json')
    assert x.status_code == 400, x.status_code


def test_message_is_arabic_for_common_errors(w):
    w.item.default_variant
    x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 99, 'delivery_location_id': w.loc.id}, format='json')
    assert any('؀' <= ch <= 'ۿ' for ch in str(x.data.get('message'))), x.data


def test_the_owner_and_a_buyer_still_open_it(w):
    res(w)
    Item.objects.filter(pk=w.item.pk).update(status='archived')
    assert api(w.seller).get(f'/api/items/{w.item.id}/').status_code == 200
    assert api(w.buyer).get(f'/api/items/{w.item.id}/').status_code == 200
    assert api(w.buyer2).get(f'/api/items/{w.item.id}/').status_code == 404


def test_an_unpublished_reel_opens_for_its_store(live):
    r = Reel.objects.create(store=live.store, status='archived', video_key='v', thumbnail_key='t', duration_seconds=1)
    assert api(live.seller).get(f'/api/reels/{r.id}/').status_code == 200


def test_editing_a_message_keeps_the_limit(w):
    chat = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=chat, sender=w.buyer, message_type='user', content='a')
    c = api(w.buyer)
    assert c.patch(f'/api/messages/{m.id}/', {'content': 'x' * 5000}, format='json').status_code == 400
    assert c.patch(f'/api/messages/{m.id}/', {'content': 'b'}, format='json').status_code == 200


def test_archived_not_in_shop(w):
    Item.objects.filter(pk=w.item.pk).update(status='archived')
    x = APIClient().get('/api/listings/fixed/')
    body = x.json(); r = body.get('results', body.get('data', []))
    assert len(r) == 0, (x.status_code, len(r))


def test_archived_not_searchable(w):
    Item.objects.filter(pk=w.item.pk).update(status='archived', title='مزيف خاص')
    hits = len(APIClient().get('/api/items/', {'search': 'مزيف'}).json().get('results', []))
    assert hits == 0, hits
