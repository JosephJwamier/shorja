"""ما كشفه الفحص السابع (2026-11-25)."""
import io
from datetime import timedelta
from decimal import Decimal as D
import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from core.models import *
from core.models import get_or_create_wallet
from core.services.moderation import suspend_user
from core.tests.test_fourth_audit import small
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.tests.test_wholesale_money import approve, ask
from core.wallet_utils import complete_reservation, release_reservation_payment
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear(); yield; cache.clear()


def staff(group):
    u = User.objects.create_user(username='staff1', password='x', is_staff=True)
    u.groups.add(Group.objects.get(name=group))
    c = Client(); c.force_login(u, backend='django.contrib.auth.backends.ModelBackend')
    return u, c


def form_of(c, url):
    r = c.get(url)
    if r.status_code in (302, 403):
        pytest.skip('no access %s %s' % (r.status_code, r.get('Location')))
    assert r.status_code == 200, r.status_code
    return r.context['adminform'].form


@pytest.mark.parametrize('field', ['is_superuser', 'is_staff', 'groups', 'user_permissions', 'topups_withdrawable', 'password'])
def test_support_cannot_grant(field):
    u, c = staff('الدعم الفني')
    f = form_of(c, reverse('admin:core_user_change', args=[u.pk]))
    assert field not in f.fields


def test_support_cannot_reset_an_admins_password():
    u, c = staff('الدعم الفني')
    a = User.objects.create_superuser(username='boss', password='Old9!long#pw')
    r = c.post(reverse('admin:auth_user_password_change', args=[a.pk]), {'password1': 'New9!long#pw', 'password2': 'New9!long#pw', 'usable_password': 'true'})
    a.refresh_from_db()
    assert not a.check_password('New9!long#pw'), r.status_code


@pytest.mark.parametrize('field', ['quantity', 'wholesale_price_per_unit', 'from_user', 'to_user', 'status', 'platform_commission_amount'])
def test_delivery_ops_cannot_rewrite_held_money(w, field):
    t = approve(w, ask(w, 2))
    u, c = staff('عمليات التوصيل')
    assert field not in form_of(c, reverse('admin:core_itemtransfer_change', args=[t.pk])).fields


def test_delivery_ops_cannot_pay_wholesale(w):
    t = approve(w, ask(w, 2))
    u, c = staff('عمليات التوصيل')
    c.post(reverse('admin:core_itemtransfer_changelist'), {'action': 'pay_the_seller', '_selected_action': [t.pk]})
    t.refresh_from_db()
    assert t.held_quantity == 2


@pytest.mark.parametrize('f', ['amount', 'user', 'status'])
def test_finance_cannot_rewrite_transactions(w, f):
    t = Transaction.objects.create(user=w.buyer, transaction_type='payment', amount=5, status='completed')
    u, c = staff('الشؤون المالية')
    assert f not in form_of(c, reverse('admin:core_transaction_change', args=[t.pk])).fields


@pytest.mark.parametrize('f', ['cash_to_collect', 'cash_collected', 'cash_due_to_platform', 'status', 'delivery_fee'])
def test_delivery_ops_cash_fields(w, f):
    co = DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    o = DeliveryOrder.objects.create(reservation=res(w), company=co, direction='outbound', status='assigned', cash_to_collect=90000)
    u, c = staff('عمليات التوصيل')
    assert f not in form_of(c, reverse('admin:core_deliveryorder_change', args=[o.pk])).fields


def test_images_per_item_are_bounded(w):
    for k in range(3):
        api(w.seller).post(f'/api/items/{w.item.id}/upload_images/', {'images': [small('PNG', f'{k}{n}.png') for n in range(20)]})
    n = ItemImage.objects.filter(item=w.item).count()
    assert 0 < n <= 20, n


def test_an_old_accepted_offer_expires(w):
    ch = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=ch, sender=w.buyer, message_type='offer', offer_amount=50000, offer_quantity=1, offer_status='accepted', content='x')
    Message.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=120))
    w.lst.price = 300000; w.lst.save()
    x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'offer_message_id': m.id}, format='json')
    assert x.status_code == 400, (x.status_code, x.data.get('data', {}).get('total_price'))


def auc(w, dep=1000):
    i = Item.objects.create(owner=w.seller, current_holder=w.seller, title='t', description='d', category=w.cat, condition='new', quantity=1, status='active')
    return AuctionListing.objects.create(item=i, starting_price=1000, current_price=1000, minimum_increment=1, bid_deposit=dep,
                                         start_time=timezone.now() - timedelta(days=1), end_time=timezone.now() + timedelta(days=1), status='active')


def bid(u, a, n):
    return api(u).post(f'/api/auctions/{a.id}/place_bid/', {'amount': str(n)}, format='json').status_code


def end(a):
    from core.tasks import process_ended_auctions
    AuctionListing.objects.filter(pk=a.pk).update(end_time=timezone.now() - timedelta(minutes=1))
    process_ended_auctions()
    a.refresh_from_db()


def test_a_banned_bidder_does_not_win(w):
    a = auc(w)
    bid(w.buyer, a, 2000); bid(w.buyer2, a, 3000)
    User.objects.filter(pk=w.buyer2.pk).update(is_active=False)
    end(a)
    assert a.winner == w.buyer, a.winner


def test_banned_sellers_auction_gives_deposits_back(w):
    a = auc(w)
    bid(w.buyer, a, 2000)
    User.objects.filter(pk=w.seller.pk).update(is_active=False)
    end(a)
    from core.tasks import auto_complete_unclaimed_auction_reservations
    Reservation.objects.update(expires_at=timezone.now() - timedelta(hours=1))
    auto_complete_unclaimed_auction_reservations()
    assert bal(w.buyer) == 5000000, (bal(w.buyer), list(Reservation.objects.values_list('status', flat=True)))


def test_banned_rep_earns_nothing(w):
    a = Representative.objects.create(item=w.item, owner=w.seller, representative=w.rep, commission_percentage=1, status='accepted')
    User.objects.filter(pk=w.rep.pk).update(is_active=False)
    x = api(w.buyer).post(f'/api/representatives/{a.id}/create_reservation_for_buyer/', {'item_id': w.item.id, 'quantity': 1}, format='json')
    assert x.status_code != 201


def test_suspending_ends_the_live(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    l = LiveSession.objects.create(store=s, title='t', status='live', started_at=timezone.now())
    suspend_user(w.seller, reason='fraud', actor=w.rep)
    l.refresh_from_db()
    assert l.status != 'live', l.status


def test_paid_order_of_a_banned_seller_can_be_cancelled(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    suspend_user(w.seller, reason='fraud', actor=w.rep)
    x = api(w.buyer).post(f'/api/reservations/{r.id}/cancel/', {}, format='json')
    assert x.status_code == 200 and bal(w.buyer) == 5000000, (x.status_code, ascii(x.data.get('message')))


def test_exports_escape_formulas(settings):
    assert getattr(settings, 'IMPORT_EXPORT_ESCAPE_FORMULAE_ON_EXPORT', False)


def test_coupon_never_below_zero(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    cp = StoreCoupon.objects.create(store=s, code='A', amount=500000, min_order_amount=0)
    cc = CouponCollection.objects.create(coupon=cp, user=w.buyer)
    x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'coupon_collection_id': cc.id}, format='json')
    r = Reservation.objects.filter(buyer=w.buyer).first()
    assert x.status_code == 400 or r.total_price >= 0, r.total_price


def test_held_goods_archived_by_the_reseller(w):
    t = approve(w, ask(w, 2))
    api(w.buyer).delete(f'/api/items/{t.resulting_item_id}/')
    api(User.objects.get(pk=w.buyer.pk)).post(f'/api/transfers/{t.id}/request_refund/')
    y = api(User.objects.get(pk=w.seller.pk)).post(f'/api/transfers/{t.id}/accept_refund/')
    i = Item.objects.get(pk=t.resulting_item_id)
    assert y.status_code == 200 and bal(w.buyer) == 5000000, (y.status_code, i.status)


def test_support_still_suspends_and_verifies_a_user(w):
    u, c = staff('الدعم الفني')
    url = reverse('admin:core_user_changelist')
    c.post(url, {'action': 'verify_identity_action', '_selected_action': [w.buyer.pk]})
    c.post(url, {'action': 'suspend_users_action', '_selected_action': [w.buyer.pk], 'apply': '1', 'reason': 'x'})
    w.buyer.refresh_from_db()
    assert w.buyer.identity_verified and not w.buyer.is_active


def test_support_cannot_suspend_an_admin():
    u, c = staff('الدعم الفني')
    a = User.objects.create_superuser(username='boss', password='Old9!long#pw')
    c.post(reverse('admin:core_user_changelist'), {'action': 'suspend_users_action', '_selected_action': [a.pk], 'apply': '1', 'reason': 'x'})
    a.refresh_from_db()
    assert a.is_active
    assert c.get(reverse('admin:core_user_delete', args=[a.pk])).status_code == 403


def test_the_owner_keeps_the_whole_user_page():
    a = User.objects.create_superuser(username='boss', password='Old9!long#pw')
    c = Client(); c.force_login(a, backend='django.contrib.auth.backends.ModelBackend')
    f = form_of(c, reverse('admin:core_user_change', args=[a.pk]))
    assert {'is_superuser', 'topups_withdrawable', 'phone'} <= set(f.fields)


def test_finance_settles_held_money(w):
    t = approve(w, ask(w, 2))
    u, c = staff('الشؤون المالية')
    c.post(reverse('admin:core_itemtransfer_changelist'), {'action': 'pay_the_seller', '_selected_action': [t.pk]})
    t.refresh_from_db()
    assert t.held_quantity == 0


def test_a_fresh_offer_still_works(w):
    ch = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=ch, sender=w.buyer, message_type='offer', offer_amount=50000, offer_quantity=1, offer_status='pending', content='x')
    Message.objects.filter(pk=m.pk).update(created_at=timezone.now() - timedelta(days=30))
    assert api(w.seller).post(f'/api/messages/{m.id}/accept_offer/').status_code == 200
    x = api(w.buyer).post('/api/reservations/create_reservation/', {'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id, 'offer_message_id': m.id}, format='json')
    assert x.status_code == 201, x.data


def test_nobody_left_to_win(w):
    a = auc(w)
    bid(w.buyer, a, 2000)
    User.objects.filter(pk=w.buyer.pk).update(is_active=False)
    end(a)
    assert a.winner is None and bal(w.buyer) == 5000000 and not Reservation.objects.exists()


def test_only_our_formats_are_decoded():
    from django.core.exceptions import ValidationError
    from core.uploads import validate_image
    with pytest.raises(ValidationError):
        validate_image(small('BMP', 'x.png'))
    validate_image(small('PNG', 'x.png'))


# ---- blocking

def chat(w):
    return Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)


def say(u, ch):
    return api(u).post(f'/api/chats/{ch.id}/send_message/', {'content': 'x'}, format='json').status_code


def test_a_block_stops_both_sides_and_is_lifted_by_its_maker(w):
    ch = chat(w)
    assert say(w.buyer, ch) == 201
    assert api(w.seller).post(f'/api/chats/{ch.id}/block/').status_code == 200
    assert (say(w.buyer, ch), say(w.seller, ch)) == (403, 403)
    assert api(w.buyer).post(f'/api/chats/{ch.id}/send_offer/', {'offer_amount': '5', 'quantity': 1}, format='json').status_code == 403
    mine, theirs = (api(u).get(f'/api/chats/{ch.id}/').data for u in (w.seller, w.buyer))
    assert (mine['blocked_by_me'], mine['can_message']) == (True, False)
    assert (theirs['blocked_by_me'], theirs['can_message']) == (False, False)
    assert [x['chat_id'] for x in api(w.seller).get('/api/chats/blocked/').data['data']] == [ch.id]
    api(w.buyer).post(f'/api/chats/{ch.id}/unblock/')
    assert say(w.buyer, ch) == 403
    api(w.seller).post(f'/api/chats/{ch.id}/unblock/')
    assert say(w.buyer, ch) == 201


def test_a_block_covers_every_chat_between_them(w):
    s = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    api(w.seller).post(f'/api/chats/{chat(w).id}/block/')
    api(w.buyer).post('/api/chats/create_or_get_store/', {'store_id': s.id, 'initial_message': 'مرحبا'}, format='json')
    assert not Message.objects.exists()


def test_support_cannot_be_blocked(w):
    x = api(w.buyer).post('/api/chats/create_or_get_support/', {}, format='json')
    assert api(w.buyer).post(f"/api/chats/{x.data['data']['id']}/block/").status_code == 400


def test_a_stranger_blocks_nothing(w):
    assert api(w.buyer2).post(f'/api/chats/{chat(w).id}/block/').status_code == 404


@pytest.mark.django_db(transaction=True)
def test_the_open_socket_obeys_the_block():
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from core.consumers import ChatConsumer
    a, b = user('wa'), user('wb')
    cat = Category.objects.create(name='c', slug='c')
    i = Item.objects.create(owner=b, current_holder=b, title='t', description='d', category=cat, condition='new', quantity=1, status='active')
    ch = Chat.objects.create(item=i, buyer=a, seller=b)
    UserBlock.objects.create(blocker=b, blocked=a)
    con = ChatConsumer()
    con.chat_id, con.user, con.room_group_name, con.channel_layer = ch.id, a, 'x', get_channel_layer()
    sent = []

    async def send(text_data=None):
        sent.append(text_data)
    con.send = send
    async_to_sync(con.handle_chat_message)({'message': 'hi'})
    assert not Message.objects.exists() and 'blocked' in sent[0]
