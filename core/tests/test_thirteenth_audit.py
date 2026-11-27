"""ما بقي معلّقاً بلا شركة توصيل، وتذكيرات تقول ما ليس صحيحاً (2026-11-27)."""
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from core.models import DeliveryCompany, InstallmentPlan, Reservation, Wallet
from core.tasks import (
    auto_complete_unclaimed_auction_reservations, remind_to_complete_reservation,
    send_pending_reservation_reminders,
)
from core.tests.test_money_faults import bal, res, user, w, win  # noqa

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def lapsed(w):
    return win(w, expires_at=timezone.now() - timedelta(hours=1))


# ---- an unclaimed auction win, with nobody to collect the rest

def test_no_courier_the_rest_is_paid_from_the_wallet(w):
    r = lapsed(w)
    auto_complete_unclaimed_auction_reservations()
    r.refresh_from_db()
    assert (r.status, r.is_deposit_only) == ('completed', False)
    # 1000 of the bid deposit while bidding, the other 1000 now.
    assert bal(w.buyer) == D(4998000)


def test_no_courier_and_no_money_the_win_is_off(w):
    r = lapsed(w)
    Wallet.objects.filter(user=w.buyer).update(balance=0)
    with patch('core.notifications.notify_push') as push:
        auto_complete_unclaimed_auction_reservations()
    r.refresh_from_db()
    assert r.status == 'cancelled'
    assert bal(w.seller) == D(1000)  # the deposit, as when a winner backs out
    r.item.refresh_from_db()
    assert r.item.status == 'draft'
    assert {c.args[0] for c in push.call_args_list} == {w.buyer, w.seller}


def test_with_a_courier_the_deposit_default_stays(w):
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    r = lapsed(w)
    auto_complete_unclaimed_auction_reservations()
    r.refresh_from_db()
    assert (r.status, r.is_deposit_only) == ('completed', True)
    assert bal(w.buyer) == D(4999000)


# ---- a buyer waiting on an installment review is not told their order ends

def requested(w):
    r = res(w)
    InstallmentPlan.objects.create(
        reservation=r, buyer=w.buyer, provider='qicard', reference='X', state='requested', principal=1, months=10,
        customer_fee_percent=0, customer_fee_amount=0, total_payable=1, monthly_amount=1, merchant_fee_percent=0,
        merchant_fee_amount=0, platform_settlement=1, seller_fee_amount=0, expires_at=timezone.now() + timedelta(days=3))
    return r


def test_the_periodic_reminder_skips_an_installment_request(w):
    soon = timezone.now() + timedelta(hours=4)
    Reservation.objects.filter(pk=requested(w).pk).update(expires_at=soon)
    plain = res(w, buyer=user('b3', 1), expires_at=soon)
    with patch('core.notifications.notify_reservation_expiring') as remind:
        send_pending_reservation_reminders()
    assert [c.args[0].pk for c in remind.call_args_list] == [plain.pk]


def test_the_one_hour_reminder_skips_it_too(w):
    r = requested(w)
    with patch('core.notifications.notify_complete_your_reservation') as remind:
        assert 'installment' in remind_to_complete_reservation(r.id)
    remind.assert_not_called()


# ---- what the order page offers matches what the server will do

def seen_by(u, r):
    from core.tests.test_money_faults import api
    return api(u).get(f'/api/reservations/{r.id}/').data


def test_handed_over_the_buyer_can_confirm_even_with_a_courier(w):
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    r = res(w, status='completed', completed_at=timezone.now())
    assert seen_by(w.buyer, r)['can_approve'] is False
    Reservation.objects.filter(pk=r.pk).update(handed_over_at=timezone.now())
    assert seen_by(w.buyer, r)['can_approve'] is True
    assert seen_by(w.seller, r)['can_request_payment_release'] is True


def test_a_deposit_order_is_not_offered_a_confirm_it_would_refuse(w):
    r = res(w, status='completed', completed_at=timezone.now(), is_deposit_only=True)
    assert seen_by(w.buyer, r)['can_approve'] is False


# ---- a live sale with no courier is paid in full

def live_setup():
    # The helpers only: test_live_shopping's autouse courier must not come along.
    from core.tests.test_live_shopping import (
        make_category, make_item, make_queue_item, make_session, make_store, make_user,
    )
    from core.models import Location
    owner = make_user('live_owner')
    buyer = make_user('live_buyer')
    Location.objects.create(user=buyer, name='h', address_line1='x', city='Baghdad', state='بغداد',
                            country='Iraq', contact_person='p', contact_phone='1')
    session = make_session(make_store(owner), status='live')
    item = make_item(owner, make_category(), quantity=5)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    Wallet.objects.filter(user=buyer).delete()
    from core.models import get_or_create_wallet
    get_or_create_wallet(buyer).credit(D('1000'))
    return buyer, session, entry


def live_reserve(buyer, session, entry, **extra):
    from core.tests.test_live_shopping import make_screenshot
    from core.tests.test_money_faults import api
    body = {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot(), **extra}
    return api(buyer).post(f'/api/live-sessions/{session.id}/reserve/', body, format='multipart')


def test_live_without_a_courier_charges_the_whole_price(w):
    buyer, session, entry = live_setup()
    x = live_reserve(buyer, session, entry, pay_in_full='true')
    assert x.status_code == 201, x.data
    r = Reservation.objects.get(buyer=buyer)
    assert (r.status, r.is_deposit_only) == ('completed', False)
    assert bal(buyer) == D('900')


def test_an_old_app_is_told_to_update_rather_than_charged_unseen(w):
    buyer, session, entry = live_setup()
    x = live_reserve(buyer, session, entry)
    assert x.status_code == 400 and x.data['code'] == 'update_app'
    assert bal(buyer) == D('1000') and not Reservation.objects.filter(buyer=buyer).exists()


def test_the_queue_says_whether_the_price_is_paid_now(w):
    from core.serializers import LiveSessionItemSerializer
    _, _, entry = live_setup()
    assert LiveSessionItemSerializer(entry).data['pay_in_full'] is True
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    assert LiveSessionItemSerializer(entry).data['pay_in_full'] is False


# ---- token prices make neither a sale nor a store's reputation

@pytest.fixture
def real_floors(settings):
    settings.MIN_ITEM_PRICE = 250
    settings.MIN_TRUSTED_SALE = 5000


def confirmed_sale(w, price):
    r = res(w, total_price=D(price), status='confirmed', completed_at=timezone.now(),
            payment_released_to_seller=True, payment_released_at=timezone.now())
    return r


def test_a_price_under_the_floor_is_refused(w, real_floors):
    from core.serializers import FixedPriceListingCreateSerializer, MessageOfferSerializer
    assert not FixedPriceListingCreateSerializer(data={'price': '100', 'deposit_percentage': 10}).is_valid()
    assert FixedPriceListingCreateSerializer(data={'price': '250', 'deposit_percentage': 10}).is_valid()
    assert not MessageOfferSerializer(data={'offer_amount': '1', 'quantity': 1}).is_valid()
    assert MessageOfferSerializer(data={'offer_amount': '250', 'quantity': 1}).is_valid()


def test_token_sales_do_not_raise_a_store_level(w, real_floors):
    from core.models import UserStore
    from core.services.store_badges import store_stats
    store = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    for _ in range(3):
        confirmed_sale(w, 1000)
    confirmed_sale(w, 5000)
    assert store_stats(store)['sales'] == 1


def test_a_token_purchase_earns_no_review(w, real_floors):
    from core.serializers import has_bought
    confirmed_sale(w, 1000)
    assert has_bought(w.buyer, owner=w.seller) is False
    confirmed_sale(w, 5000)
    assert has_bought(w.buyer, owner=w.seller) is True


def test_an_order_handed_over_in_person_can_be_reviewed(w):
    from core.serializers import has_bought
    res(w, status='completed', completed_at=timezone.now(), handed_over_at=timezone.now())
    assert has_bought(w.buyer, item=w.item) is True


# ---- a closed store sells nothing, on air included

def closed_live(w):
    from core.models import LiveSession, UserStore
    store = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    session = LiveSession.objects.create(store=store, title='t', status='live', started_at=timezone.now())
    return store, session


def test_closing_a_store_ends_its_stream(w):
    from core.services.moderation import close_store
    store, session = closed_live(w)
    close_store(store)
    session.refresh_from_db()
    assert session.status != 'live' and not store.is_active


def test_the_owner_closing_it_ends_the_stream_too(w):
    from core.tests.test_money_faults import api
    store, session = closed_live(w)
    x = api(w.seller).post(f'/api/stores/{store.id}/toggle_active/', {'is_active': 'false'})
    assert x.status_code == 200, x.data
    store.refresh_from_db(); session.refresh_from_db()
    assert not store.is_active and session.status != 'live'


def test_a_closed_store_cannot_go_live(w):
    from core.models import LiveSession, UserStore
    from core.tests.test_money_faults import api
    UserStore.objects.create(user=w.seller, name='s', is_active=False)
    x = api(w.seller).post('/api/live-sessions/', {'title': 't', 'start_immediately': True}, format='json')
    # IsStoreOwner turns a closed store away first (403); the view's own
    # store_closed check is the same rule for a scheduled session's go_live.
    assert x.status_code in (400, 403)
    assert not LiveSession.objects.filter(store__user=w.seller).exists()


def test_a_closed_stores_stream_is_hidden(w):
    from core.models import UserStore
    from core.tests.test_money_faults import api
    store, session = closed_live(w)
    UserStore.objects.filter(pk=store.pk).update(is_active=False)
    lives = api(w.buyer).get('/api/live-sessions/').data
    rows = lives.get('results', lives) if isinstance(lives, dict) else lives
    assert all(r['id'] != session.id for r in rows)


def test_no_bid_on_a_closed_stores_auction(w):
    from core.models import UserStore
    from core.tests.test_money_faults import api, auction
    a = auction(w, deposit=0)
    a.status = 'active'; a.end_time = timezone.now() + timedelta(days=1); a.save()
    UserStore.objects.create(user=w.seller, name='s', is_active=False)
    x = api(w.buyer).post(f'/api/auctions/{a.id}/place_bid/', {'amount': '5000'}, format='json')
    assert x.status_code == 400, x.data


# ---- auction news reaches the phone, not only an open screen

def pushed(push):
    return [(c.args[0].pk, c.kwargs['data']['type']) for c in push.call_args_list]


def test_a_bid_tells_the_seller_and_the_one_overtaken(w):
    from core.tests.test_seventh_audit import auc, bid
    a = auc(w)
    with patch('core.notifications.notify_push') as push:
        assert bid(w.buyer, a, 2000) == 201
        assert bid(w.buyer2, a, 3000) == 201
    sent = pushed(push)
    assert (w.seller.pk, 'new_bid') in sent
    assert (w.buyer.pk, 'outbid') in sent
    assert (w.buyer2.pk, 'outbid') not in sent


def test_the_end_tells_the_losers_and_a_bidless_end_tells_the_seller(w):
    from core.tests.test_seventh_audit import auc, bid, end
    a = auc(w)
    bid(w.buyer, a, 2000); bid(w.buyer2, a, 3000)
    with patch('core.notifications.notify_push') as push:
        end(a)
    assert (w.buyer.pk, 'auction_lost') in pushed(push)

    empty = auc(w)
    with patch('core.notifications.notify_push') as push:
        end(empty)
    assert (w.seller.pk, 'auction_no_winner') in pushed(push)


def test_bidders_hear_once_that_the_end_is_near(w):
    from core.models import AuctionListing
    from core.tasks import remind_auctions_ending_soon
    from core.tests.test_seventh_audit import auc, bid
    a = auc(w)
    bid(w.buyer, a, 2000)
    AuctionListing.objects.filter(pk=a.pk).update(end_time=timezone.now() + timedelta(minutes=12))
    with patch('core.notifications.notify_push') as push:
        remind_auctions_ending_soon()
    assert (w.buyer.pk, 'auction_ending_soon') in pushed(push)
    AuctionListing.objects.filter(pk=a.pk).update(end_time=timezone.now() + timedelta(minutes=7))
    with patch('core.notifications.notify_push') as push:
        remind_auctions_ending_soon()
    assert push.call_count == 0


# ---- the news that moves money reaches the phone

def test_both_sides_hear_how_a_dispute_ended(w):
    from django.db import transaction
    from core.models import Dispute
    from core.services.disputes import resolve_dispute
    from core.wallet_utils import complete_reservation
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', status='open')
    Reservation.objects.filter(pk=r.pk).update(status='disputed')
    with patch('core.notifications.notify_push') as push, \
            patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
        resolve_dispute(d, resolution_action='refund_buyer', resolution='السلعة معيبة')
    sent = pushed(push)
    assert (w.buyer.pk, 'dispute_resolved') in sent and (w.seller.pk, 'dispute_resolved') in sent


def test_the_seller_hears_the_money_arrived(w):
    from django.db import transaction
    from core.tests.test_money_faults import api
    from core.wallet_utils import complete_reservation
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    from core.tasks import send_payment_release_notification
    with patch('core.notifications.notify_push') as push, \
            patch('django.db.transaction.on_commit', side_effect=lambda f: f()), \
            patch.object(send_payment_release_notification, 'delay',
                         side_effect=lambda *a, **k: send_payment_release_notification(*a, **k)):
        assert api(w.buyer).post(f'/api/reservations/{r.id}/approve_and_release_payment/').status_code == 200
    assert (w.seller.pk, 'payment_released') in pushed(push)


def test_an_expired_reservation_tells_its_buyer(w):
    from core.tasks import process_expired_reservations
    r = res(w, expires_at=timezone.now() - timedelta(hours=1))
    with patch('core.notifications.notify_push') as push, \
            patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
        process_expired_reservations()
    assert (w.buyer.pk, 'reservation_expired') in pushed(push)
    r.refresh_from_db()
    assert r.status == 'expired'


# ---- a coupon the platform grants comes out of the platform's money

def platform_coupon_sale(w, funded=True):
    from django.db import transaction
    from core.models import CouponCollection, StoreCoupon, UserStore
    from core.wallet_utils import complete_reservation
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    c = StoreCoupon.objects.create(store=st, code='G', amount=D(5000), min_order_amount=0, platform_funded=funded)
    from core.tests.test_money_faults import P
    r = res(w, total_price=P - D(5000), coupon_discount=D(5000), platform_commission_amount=D(0),
            applied_coupon=CouponCollection.objects.create(coupon=c, user=w.buyer))
    with transaction.atomic():
        complete_reservation(r, 'full')
    return r


def test_the_seller_gets_the_full_price_of_a_platform_coupon_sale(w):
    from django.db import transaction
    from core.services.commission import platform_account
    from core.tests.test_money_faults import P, total
    from core.wallet_utils import release_reservation_payment
    r = platform_coupon_sale(w)
    # The buyer's payment sits in escrow, in no wallet, until release.
    before = total() + r.amount_charged
    with transaction.atomic():
        release_reservation_payment(r)
    assert bal(w.seller) == P            # the price the buyer paid, and the discount
    assert bal(platform_account()) == D(-5000)
    assert total() == before              # money moved, none was made


def test_a_stores_own_coupon_is_still_the_stores(w):
    from django.db import transaction
    from core.tests.test_money_faults import P
    from core.wallet_utils import release_reservation_payment
    r = platform_coupon_sale(w, funded=False)
    with transaction.atomic():
        release_reservation_payment(r)
    assert bal(w.seller) == P - D(5000)


def test_a_refund_after_release_gives_the_platform_its_discount_back(w):
    from django.db import transaction
    from core.services.commission import platform_account
    from core.wallet_utils import claw_back_release, release_reservation_payment
    r = platform_coupon_sale(w)
    with transaction.atomic():
        release_reservation_payment(r)
        claw_back_release(r, r.amount_charged)
    assert bal(platform_account()) == D(0) and bal(w.seller) == D(0)


# ---- the schedule lives in one place, and the setup command is safe to repeat

def test_store_levels_and_the_weekly_cleanups_are_scheduled():
    from django.conf import settings
    tasks = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
    assert {'core.tasks.recompute_store_levels', 'core.tasks.purge_archived_items',
            'core.tasks.decay_category_affinities'} <= tasks


def test_running_setup_celery_tasks_twice_keeps_every_task(db):
    from django.conf import settings
    from django.core.management import call_command
    from django_celery_beat.models import PeriodicTask
    call_command('setup_celery_tasks')
    call_command('setup_celery_tasks')
    names = set(PeriodicTask.objects.values_list('name', flat=True))
    assert set(settings.CELERY_BEAT_SCHEDULE) <= names


# ---- held wholesale money: the other side hears when it moves

def test_the_seller_hears_the_held_money_was_released(w):
    from core.tests.test_wholesale_money import act, approve, ask
    t = approve(w, ask(w, 2))
    with patch('core.notifications.notify_push') as push, \
            patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
        act(w.buyer, t, 'confirm_received')
    assert any(c.args[0].pk == w.seller.pk and 'transfer_id' in c.kwargs['data']
               for c in push.call_args_list)


def test_the_reseller_hears_the_refund_came_back(w):
    from core.tests.test_wholesale_money import act, approve, ask
    t = approve(w, ask(w, 2))
    act(w.buyer, t, 'request_refund')
    with patch('core.notifications.notify_push') as push, \
            patch('django.db.transaction.on_commit', side_effect=lambda f: f()):
        act(w.seller, t, 'accept_refund')
    assert any(c.args[0].pk == w.buyer.pk for c in push.call_args_list)


# ---- a reservation made through a representative reaches the owner's phone

def test_the_owner_is_pushed_about_a_representative_reservation(w):
    from core.tasks import send_reservation_created_notification
    r = res(w, representative=w.rep)
    with patch('core.notifications.notify_push') as push:
        send_reservation_created_notification(r.id, w.seller.id)
    assert any(c.args[0].pk == w.seller.pk for c in push.call_args_list)
