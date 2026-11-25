"""ثغرات كشفها الفحص الثاني (2026-11-24)، كلّ اختبار يثبّت السلوك الصحيح."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.delivery_utils import combine_deliveries, create_outbound_delivery_order
from core.models import (
    AuctionListing, Bid, Chat, DeliveryCompany, DeliveryOrder, Dispute, Item, ItemVariant, Location,
    Message, ProductReview, Rating, Reservation, StoreCoupon, StoreReview, User, UserStore,
    WithdrawalRequest, get_or_create_wallet,
)
from core.services.commission import platform_account
from core.services.moderation import archive_item
from core.tasks import release_payments_after_week
from core.services.disputes import resolve_dispute
from core.services.withdrawals import approve_withdrawal
from core.wallet_utils import complete_reservation, release_reservation_payment
from core.tests.test_money_faults import P, api, bal, rep_res, res, total, user, w  # noqa

pytestmark = pytest.mark.django_db
FAR = [c[0] for c in Location._meta.get_field('state').choices if c[0] != 'baghdad'][0]


def company(w, **k):
    User.objects.filter(username='se').update(phone='07701234567'); User.objects.filter(username='bu').update(phone='07801234567')
    w.seller.phone = w.buyer.phone = '07701234567'
    Location.objects.create(user=w.seller, name='wh', address_line1='a', city='b', state=w.loc.state, contact_person='p', contact_phone='07701234567')
    return DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True,
                                          intra_governorate_fee=5000, inter_governorate_fee=10000, **k)


def test_platform_username_cannot_be_taken():
    User.objects.filter(username='platform').update(username='gone')
    u = User.objects.create_user(username='platform', password='x')   # an ordinary sign-up
    assert platform_account().pk != u.pk


def test_the_platform_account_exists_and_cannot_sign_in():
    account = platform_account()
    assert account.username == 'platform' and not account.is_active
    assert not account.has_usable_password()


def test_register_endpoint_refuses_reserved_username():
    User.objects.filter(username='platform').update(username='gone')
    r = APIClient().post('/api/auth/register/', {
        'username': 'Platform', 'password': 'Str0ng!pass99', 'password2': 'Str0ng!pass99',
        'password_confirm': 'Str0ng!pass99', 'email': 'a@b.co', 'phone': '07701234567',
        'first_name': 'a', 'last_name': 'b', 'accept_terms': True, 'terms_accepted': True}, format='json')
    assert not User.objects.filter(username='platform').exists(), r.status_code


def test_nobody_renames_themselves_to_a_reserved_name(w):
    User.objects.filter(username='platform').update(username='gone')
    for name in ('platform', ' PLATFORM', 'الشورجة', 'support'):
        r = api(w.buyer).patch(f'/api/users/{w.buyer.id}/', {'username': name}, format='json')
        assert r.status_code == 400, name
    assert api(w.buyer).patch(f'/api/users/{w.buyer.id}/', {'username': 'ali_1'}, format='json').status_code == 200


def cod(w, c):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'deposit')
    o = create_outbound_delivery_order(r)
    o.status = 'delivered'
    o.delivered_at = timezone.now() - timedelta(days=30)
    o.save()
    return r, o


def test_cod_remainder_reaches_the_seller_without_the_buyers_tap(w):
    r, o = cod(w, company(w))
    release_payments_after_week()
    r.refresh_from_db()
    o.refresh_from_db()
    assert bal(w.seller) == D(97000)
    assert (r.status, o.status, o.cash_collected) == ('confirmed', 'confirmed', True)
    api(w.buyer).post(f'/api/delivery-orders/{o.id}/confirm-receipt/')
    release_payments_after_week()
    assert bal(w.seller) == D(97000)


def test_a_dispute_decided_for_the_seller_pays_the_cash_too(w):
    r, o = cod(w, company(w))
    d = Dispute.objects.create(item=w.item, reservation=r, complainant=w.buyer, defendant=w.seller,
                               title='t', description='d', reason='other')
    resolve_dispute(d, resolution_action='pay_seller')
    assert bal(w.seller) == D(97000)


def prepaid_delivered(w):
    c = company(w)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    o = create_outbound_delivery_order(r)
    o.status = 'delivered'; o.delivered_at = timezone.now() - timedelta(days=30); o.save()
    return c, r, o


def test_refusing_after_release_creates_no_money(w):
    before = total()
    c, r, o = prepaid_delivered(w)
    release_payments_after_week()
    x = api(w.buyer).post(f'/api/delivery-orders/{o.id}/refuse-receipt/', {}, format='json')
    ret = DeliveryOrder.objects.filter(direction='return').first()
    y = ret and api(c.account).post(f'/api/delivery-orders/{ret.id}/pick-up/')
    assert not y and total() <= before and x.status_code == 400


def test_an_open_delivery_can_still_be_refused(w):
    c, r, o = prepaid_delivered(w)
    x = api(w.buyer).post(f'/api/delivery-orders/{o.id}/refuse-receipt/', {}, format='json')
    assert x.status_code == 200


def test_address_change_after_paying_reprices(w):
    company(w)
    Location.objects.create(user=w.seller, name='s', address_line1='a', city='b', state=w.loc.state,
                            contact_person='p', contact_phone='07701234567', is_primary=True)
    far = Location.objects.create(user=w.buyer, name='f', address_line1='a', city='b', state=FAR,
                                  contact_person='p', contact_phone='07701234567')
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    paid = r.delivery_fee
    x = api(w.buyer).post(f'/api/reservations/{r.id}/update-delivery-location/', {'delivery_location_id': far.id})
    r.refresh_from_db()
    assert (x.status_code, paid, r.delivery_fee) == (200, D(5000), D(10000))
    assert bal(w.buyer) == D(5000000) - P - D(10000)
    api(w.buyer).post(f'/api/reservations/{r.id}/update-delivery-location/', {'delivery_location_id': w.loc.id})
    assert bal(w.buyer) == D(5000000) - P - D(5000)


def test_an_address_the_wallet_cannot_cover_is_refused(w):
    company(w)
    far = Location.objects.create(user=w.buyer, name='f', address_line1='a', city='b', state=FAR,
                                  contact_person='p', contact_phone='07701234567')
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    get_or_create_wallet(w.buyer).debit(bal(w.buyer))
    x = api(w.buyer).post(f'/api/reservations/{r.id}/update-delivery-location/', {'delivery_location_id': far.id})
    r.refresh_from_db()
    assert (x.status_code, r.delivery_location_id, r.delivery_fee) == (400, w.loc.id, D(5000))


def test_editing_the_saved_address_after_paying(w):
    company(w)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    x = api(w.buyer).patch(f'/api/locations/{w.loc.id}/', {'state': FAR}, format='json')
    w.loc.refresh_from_db()
    assert (x.status_code, w.loc.state) == (400, 'baghdad')
    assert api(w.buyer).patch(f'/api/locations/{w.loc.id}/', {'address_line1': 'دار 5'}, format='json').status_code == 200


def test_offer_is_for_one_variant(w):
    w.item.options = [{'name': 's', 'values': ['S', 'XL']}]; w.item.save()
    cheap = ItemVariant.objects.create(item=w.item, attributes={'s': 'S'}, quantity=3, price=D(10000))
    dear = ItemVariant.objects.create(item=w.item, attributes={'s': 'XL'}, quantity=3, price=D(900000))
    chat = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    m = Message.objects.create(chat=chat, sender=w.buyer, message_type='offer', content='x', offer_amount=D(9000),
                               offer_quantity=1, offer_status='accepted')
    x = api(w.buyer).post('/api/reservations/create_reservation/', {
        'item_id': w.item.id, 'quantity': 1, 'variant_id': dear.id, 'offer_message_id': m.id,
        'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 400
    m.offer_variant = cheap
    m.save()
    x = api(w.buyer).post('/api/reservations/create_reservation/', {
        'item_id': w.item.id, 'quantity': 1, 'variant_id': cheap.id, 'offer_message_id': m.id,
        'delivery_location_id': w.loc.id}, format='json')
    assert x.status_code == 201, x.data


def test_an_offer_names_its_variant(w):
    w.item.options = [{'name': 's', 'values': ['S']}]
    w.item.save()
    v = ItemVariant.objects.create(item=w.item, attributes={'s': 'S'}, quantity=3, price=D(10000))
    chat = Chat.objects.create(item=w.item, buyer=w.buyer, seller=w.seller)
    url = f'/api/chats/{chat.id}/send_offer/'
    assert api(w.buyer).post(url, {'offer_amount': '9000', 'quantity': 1}, format='json').status_code == 400
    x = api(w.buyer).post(url, {'offer_amount': '9000', 'quantity': 1, 'variant_id': v.id}, format='json')
    assert (x.status_code, x.data['data']['offer_variant']) == (201, v.id)
    assert 'S' in Message.objects.get().content


def test_warranty_cannot_shrink_after_reservation(w):
    w.item.warranty_days = 30; w.item.save()
    x = api(w.buyer).post('/api/reservations/create_reservation/', {
        'item_id': w.item.id, 'quantity': 1, 'delivery_location_id': w.loc.id}, format='json')
    r = Reservation.objects.get()
    api(w.seller).patch(f'/api/items/{w.item.id}/', {'warranty_days': 1}, format='json')
    api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'full'}, format='json')
    r.refresh_from_db()
    assert r.warranty_days == 30, (r.status, r.warranty_days)


def test_moderated_item_stays_archived(w):
    archive_item(w.item, reason='fake', actor=w.rep)
    x = api(w.seller).patch(f'/api/items/{w.item.id}/', {'quantity': 4}, format='json')
    w.item.refresh_from_db()
    assert w.item.status == 'archived', (x.status_code, w.item.status)


def test_moderated_item_stays_archived_on_cancel(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    archive_item(Item.objects.get(pk=w.item.pk), reason='fake', actor=w.rep)
    api(w.buyer).post(f'/api/reservations/{r.id}/cancel/')
    w.item.refresh_from_db()
    assert w.item.status == 'archived'


def test_archived_item_cannot_be_paid_for(w):
    r = res(w)
    archive_item(Item.objects.get(pk=w.item.pk), reason='fake', actor=w.rep)
    x = api(w.buyer).post(f'/api/reservations/{r.id}/complete/', {'payment_type': 'full'}, format='json')
    assert x.status_code == 400, x.status_code


def test_seller_cannot_feature_own_item(w):
    x = api(w.seller).patch(f'/api/items/{w.item.id}/', {'is_daily_deal': True}, format='json')
    w.item.refresh_from_db()
    assert not w.item.is_daily_deal, x.status_code


def test_rating_needs_a_purchase(w):
    x = api(w.buyer2).post('/api/ratings/', {'rated_user_id': w.seller.id, 'score': 1, 'rating_type': 'seller',
                                             'comment': 'x'}, format='json')
    assert x.status_code >= 400, x.status_code


def test_rating_patch_is_validated(w):
    rt = Rating.objects.create(rater=w.buyer, rated_user=w.seller, rating_type='seller', score=1, comment='x')
    x = api(w.buyer).patch(f'/api/ratings/{rt.id}/', {'score': 100}, format='json')
    rt.refresh_from_db()
    assert rt.score <= 5, rt.score


def test_product_review_needs_a_purchase(w):
    x = api(w.buyer2).post('/api/product-reviews/', {'item': w.item.id, 'score': 1, 'comment': 'x'}, format='json')
    assert not ProductReview.objects.exists(), x.status_code


def test_store_review_needs_a_purchase(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    x = api(w.buyer2).post('/api/store-reviews/', {'store': st.id, 'score': 1, 'comment': 'x'}, format='json')
    assert not StoreReview.objects.exists(), x.status_code


def test_coupon_amount_must_be_positive(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    x = api(w.seller).post(f'/api/stores/{st.id}/coupons/', {'code': 'N', 'amount': '-50000',
                                                             'min_order_amount': '0'}, format='json')
    assert not StoreCoupon.objects.filter(amount__lt=0).exists(), x.status_code


def test_variant_price_must_be_positive(w):
    x = api(w.seller).patch(f'/api/items/{w.item.id}/', {
        'options': [{'name': 's', 'values': ['S']}],
        'variants': [{'attributes': {'s': 'S'}, 'quantity': 1, 'price': '-1000'}]}, format='json')
    assert not ItemVariant.objects.filter(price__lt=0).exists()


def test_deposit_percentage_bounds(w):
    for v in ('500', '-20'):
        x = api(w.seller).patch(f'/api/items/{w.item.id}/update_fixed_listing/', {'deposit_percentage': v}, format='json')
        w.lst.refresh_from_db()
        assert 0 <= w.lst.deposit_percentage <= 100, (x.status_code, w.lst.deposit_percentage)


def bought(w, buyer):
    r = res(w, buyer=buyer)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed', completed_at=timezone.now())
    return r


def test_a_buyer_can_rate_and_review(w):
    st = UserStore.objects.create(user=w.seller, name='s', is_active=True)
    bought(w, w.buyer2)
    c = api(w.buyer2)
    assert c.post('/api/ratings/', {'rated_user_id': w.seller.id, 'score': 5, 'rating_type': 'seller',
                                    'comment': 'x'}, format='json').status_code == 201
    assert c.post('/api/product-reviews/', {'item': w.item.id, 'score': 5}, format='json').status_code == 201
    assert c.post('/api/store-reviews/', {'store': st.id, 'score': 5}, format='json').status_code == 201


def test_editing_a_review_cannot_move_it_to_another_item(w):
    bought(w, w.buyer2)
    rv = ProductReview.objects.create(reviewer=w.buyer2, item=w.item, score=1)
    other = Item.objects.create(owner=w.seller, current_holder=w.seller, title='o', description='d',
                                category=w.cat, condition='new', quantity=1, status='active')
    api(w.buyer2).patch(f'/api/product-reviews/{rv.id}/', {'item': other.id, 'score': 2}, format='json')
    rv.refresh_from_db()
    assert (rv.item_id, rv.score) == (w.item.id, 2)


def test_a_fresh_settled_sale_can_be_disputed(w):
    r = bought(w, w.buyer)
    x = api(w.buyer).post('/api/disputes/create_dispute/', {'item_id': w.item.id, 'reservation_id': r.id,
                          'title': 't', 'description': 'd', 'reason': 'other'}, format='json')
    assert x.status_code == 201, x.data


def test_old_settled_sale_cannot_be_disputed(w):
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full'); release_reservation_payment(r)
    Reservation.objects.filter(pk=r.pk).update(status='confirmed', completed_at=timezone.now() - timedelta(days=700),
                                               payment_released_at=timezone.now() - timedelta(days=690))
    x = api(w.buyer).post('/api/disputes/create_dispute/', {'item_id': w.item.id, 'reservation_id': r.id,
                          'title': 't', 'description': 'd', 'reason': 'other'}, format='json')
    assert x.status_code >= 400


def withdraw(w, **k):
    body = {'amount': '50000', 'account_number': '6330 1234 5678 9012', 'holder_name': 'علي حسن', **k}
    return api(w.buyer).post('/api/wallet/withdraw/', {a: b for a, b in body.items() if b is not None}, format='json')


@pytest.mark.parametrize('bad', [
    {'amount': '0'}, {'amount': '-5'}, {'account_number': None}, {'account_number': '12'},
    {'account_number': 'abcd1234567890'}, {'holder_name': None}, {'holder_name': ''}])
def test_a_withdrawal_says_where_to(w, bad):
    assert withdraw(w, **bad).status_code == 400
    assert not WithdrawalRequest.objects.exists()


def test_the_account_number_is_for_the_admin_and_only_until_the_decision(w):
    User.objects.filter(pk=w.buyer.pk).update(phone='+9647801234567', phone_verified=True)
    w.buyer.refresh_from_db()
    x = withdraw(w)
    wd = WithdrawalRequest.objects.get()
    assert (x.status_code, wd.account_number, wd.card_last4) == (201, '6330123456789012', '9012')
    assert '6330123456789012' not in str(x.data)
    assert '6330123456789012' not in str(api(w.buyer).get('/api/wallet/my_withdrawals/').data)
    approve_withdrawal(wd, reviewed_by=w.seller)
    wd.refresh_from_db()
    assert (wd.account_number, wd.card_last4, wd.status) == ('', '9012', 'approved')
    from auditlog.models import LogEntry
    assert not any('6330123456789012' in str(e.changes) for e in LogEntry.objects.all())


def test_batch_cancel_keeps_the_courier_fee(w):
    c = company(w)
    rs = []
    for _ in range(2):
        r = res(w)
        with transaction.atomic():
            complete_reservation(r, 'full')
        rs.append(r)
    fee = rs[0].delivery_fee
    with transaction.atomic():
        combine_deliveries([Reservation.objects.get(pk=r.pk) for r in rs], c)
    api(w.seller).post(f'/api/reservations/{rs[0].id}/cancel/')
    other = Reservation.objects.get(pk=rs[1].pk)
    assert other.delivery_fee == fee and fee > 0, (other.delivery_fee, fee)


def test_delete_account_leaving_live_bids(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1,
                                      bid_deposit=0, start_time=timezone.now() - timedelta(days=1),
                                      end_time=timezone.now() + timedelta(days=1), status='active')
    b = user('poor')
    Bid.objects.create(auction=a, bidder=b, amount=5000, is_winning=True)
    x = api(b).post('/api/users/delete-account/')
    assert x.status_code == 400


def test_seller_deletes_account_with_running_auction(w):
    AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1,
                                  bid_deposit=0, start_time=timezone.now() - timedelta(days=1),
                                  end_time=timezone.now() + timedelta(days=1), status='active')
    x = api(w.seller).post('/api/users/delete-account/')
    assert x.status_code == 400


def test_title_frozen_while_an_auction_has_bids(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1,
                                      bid_deposit=0, start_time=timezone.now() - timedelta(days=1),
                                      end_time=timezone.now() + timedelta(days=1), status='active')
    Bid.objects.create(auction=a, bidder=w.buyer, amount=5000, is_winning=True)
    x = api(w.seller).patch(f'/api/items/{w.item.id}/', {'title': 'other thing'}, format='json')
    assert x.status_code == 400, x.status_code


def test_auction_item_with_bids_cannot_be_deleted(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1,
                                      bid_deposit=0, start_time=timezone.now() - timedelta(days=1),
                                      end_time=timezone.now() + timedelta(days=1), status='active')
    Bid.objects.create(auction=a, bidder=w.buyer, amount=5000, is_winning=True)
    x = api(w.seller).delete(f'/api/items/{w.item.id}/')
    assert x.status_code >= 400, x.status_code


def test_bids_gone_the_item_is_editable_again(w):
    a = AuctionListing.objects.create(item=w.item, starting_price=1000, current_price=1000, minimum_increment=1,
                                      bid_deposit=0, start_time=timezone.now() - timedelta(days=1),
                                      end_time=timezone.now() + timedelta(days=1), status='ended')
    Bid.objects.create(auction=a, bidder=w.buyer, amount=5000, is_winning=True)
    assert api(w.seller).patch(f'/api/items/{w.item.id}/', {'title': 'x'}, format='json').status_code == 200


def test_a_seller_with_no_phone_or_address_can_still_ship(w):
    DeliveryCompany.objects.create(name='dc', account=user('dc'), is_active=True)
    r = res(w)
    with transaction.atomic():
        complete_reservation(r, 'full')
    assert create_outbound_delivery_order(r).from_phone == ''


def test_changing_phone_drops_the_verified_mark(w):
    User.objects.filter(pk=w.buyer.pk).update(phone='+9647801234567', phone_verified=True, email='a@b.co', email_verified=True)
    u = User.objects.get(pk=w.buyer.pk)
    x = api(u).patch(f'/api/users/{u.id}/', {'phone': '07901112233', 'email': 'ceo@big.com'}, format='json')
    u.refresh_from_db()
    assert not (u.phone.endswith('7901112233') and u.phone_verified)
    assert not (u.email == 'ceo@big.com' and u.email_verified)
