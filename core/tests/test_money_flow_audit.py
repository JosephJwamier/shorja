"""Money-conservation checks found during the 2026-09 money-flow audit.

Each test pins one invariant that the code was found to violate: money that
appears from nowhere, money credited twice, or money that vanishes. They are
written against the API where a user could trigger the flaw, and against the
service layer where only a race or a task could.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuctionListing, Bid, Category, FixedPriceListing, Item, Location, Reservation, User,
    get_or_create_wallet,
)
from core.tasks import process_ended_auctions

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def make_user(username, balance='0'):
    user = User.objects.create_user(username=username, password='pass12345')
    if Decimal(balance) > 0:
        get_or_create_wallet(user).credit(Decimal(balance))
    return user


def make_item(owner):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(owner=owner, title='lamp', description='d', category=category,
                               condition='new', quantity=1, status='active')


def make_auction(owner, deposit='1000.00'):
    return AuctionListing.objects.create(
        item=make_item(owner), starting_price=Decimal('100.00'), current_price=Decimal('100.00'),
        minimum_increment=Decimal('10.00'), bid_deposit=Decimal(deposit),
        start_time=timezone.now() - timedelta(minutes=5),
        end_time=timezone.now() + timedelta(minutes=5), status='active',
    )


def balance(user):
    return get_or_create_wallet(user).balance


def bid(client, user, auction, amount):
    client.force_authenticate(user=user)
    response = client.post(f'/api/auctions/{auction.id}/place_bid/', {'amount': amount}, format='json')
    assert response.status_code in (200, 201), response.data
    return response


# ---------------------------------------------------------------------------
# Auctions: the bid deposit is charged once per bidder and must come back
# once per bidder — never once per bid.
# ---------------------------------------------------------------------------

def test_losing_bidder_who_bid_many_times_is_refunded_exactly_once(client):
    seller = make_user('aud_seller')
    loser = make_user('aud_loser', balance='10000')
    winner = make_user('aud_winner', balance='10000')
    auction = make_auction(seller)

    bid(client, loser, auction, '110')
    bid(client, winner, auction, '120')
    bid(client, loser, auction, '130')
    bid(client, winner, auction, '140')
    bid(client, loser, auction, '150')
    bid(client, winner, auction, '160')
    assert balance(loser) == Decimal('9000'), 'deposit charged once'
    assert balance(winner) == Decimal('9000'), 'deposit charged once'

    AuctionListing.objects.filter(pk=auction.pk).update(end_time=timezone.now() - timedelta(seconds=1))
    process_ended_auctions()

    # The loser gets their one deposit back — not one per bid.
    assert balance(loser) == Decimal('10000')
    # The winner's deposit stays committed to the sale; their earlier,
    # outbid rows must not be "refunded" as if they were losers.
    assert balance(winner) == Decimal('9000')
    assert Reservation.objects.get(auction=auction, buyer=winner).deposit_amount == Decimal('1000')


def test_ending_an_auction_twice_refunds_nobody_twice():
    seller = make_user('aud2_seller')
    loser = make_user('aud2_loser', balance='5000')
    winner = make_user('aud2_winner', balance='5000')
    auction = make_auction(seller)
    for user, amount in ((loser, '110'), (winner, '120')):
        get_or_create_wallet(user).debit(auction.bid_deposit)
        Bid.objects.create(auction=auction, bidder=user, amount=Decimal(amount),
                           deposit_paid=True, is_winning=(user is winner))
    AuctionListing.objects.filter(pk=auction.pk).update(end_time=timezone.now() - timedelta(seconds=1))

    process_ended_auctions()
    # A second sweep (or a concurrent finalizer) must be a no-op.
    AuctionListing.objects.filter(pk=auction.pk).update(status='active')
    process_ended_auctions()

    assert balance(loser) == Decimal('5000')
    assert Reservation.objects.filter(auction=auction).count() == 1


# ---------------------------------------------------------------------------
# Wallet: no endpoint may mint balance
# ---------------------------------------------------------------------------

def test_the_simulated_deposit_endpoint_is_gone(client):
    user = make_user('aud_mint')
    client.force_authenticate(user=user)
    response = client.post('/api/wallet/deposit/', {'amount': '1000000'}, format='json')
    assert response.status_code in (404, 405), response.status_code
    assert balance(user) == Decimal('0')


# ---------------------------------------------------------------------------
# Reservations: every money move is idempotent under a lock
# ---------------------------------------------------------------------------

from core.wallet_utils import complete_reservation, release_reservation_payment  # noqa: E402


def make_listing(owner, price='100.00'):
    item = make_item(owner)
    FixedPriceListing.objects.create(item=item, price=Decimal(price), deposit_percentage=10)
    return item


def make_reservation(item, buyer, **extra):
    location = Location.objects.create(
        user=buyer, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1')
    defaults = dict(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status='pending', expires_at=timezone.now() + timedelta(days=1),
        delivery_location=location,
    )
    defaults.update(extra)
    return Reservation.objects.create(**defaults)


def test_completing_a_reservation_twice_charges_once():
    seller, buyer = make_user('aud_c_s'), make_user('aud_c_b', balance='1000')
    reservation = make_reservation(make_listing(seller), buyer)
    complete_reservation(reservation, 'full')
    stale = Reservation.objects.get(pk=reservation.pk)
    stale.status = 'pending'  # what a second, stale request still believes
    with pytest.raises(ValueError):
        complete_reservation(stale, 'full')
    assert balance(buyer) == Decimal('900')
    assert reservation.item.default_variant.quantity == 0


def test_releasing_payment_twice_pays_the_seller_once():
    seller, buyer = make_user('aud_r_s'), make_user('aud_r_b', balance='1000')
    reservation = make_reservation(make_listing(seller), buyer)
    complete_reservation(reservation, 'full')
    release_reservation_payment(reservation)
    stale = Reservation.objects.get(pk=reservation.pk)
    stale.payment_released_to_seller = False
    release_reservation_payment(stale)
    assert balance(seller) == Decimal('100')


def test_cancelling_twice_refunds_once(client):
    seller, buyer = make_user('aud_x_s'), make_user('aud_x_b', balance='1000')
    reservation = make_reservation(make_listing(seller), buyer)
    complete_reservation(reservation, 'full')
    client.force_authenticate(user=buyer)
    assert client.post(f'/api/reservations/{reservation.id}/cancel/').status_code == 200
    second = client.post(f'/api/reservations/{reservation.id}/cancel/')
    assert second.status_code == 400
    assert balance(buyer) == Decimal('1000')


# ---------------------------------------------------------------------------
# Coupons: the deposit can never exceed what is owed
# ---------------------------------------------------------------------------

def test_a_large_coupon_caps_the_deposit_at_the_discounted_total(client):
    from core.models import CouponCollection, StoreCoupon, UserStore
    seller, buyer = make_user('aud_cp_s'), make_user('aud_cp_b')
    store = UserStore.objects.create(user=seller, name='s')
    item = make_listing(seller, price='10000.00')
    listing = item.fixed_listing
    listing.deposit_percentage = Decimal('50'); listing.save()
    coupon = StoreCoupon.objects.create(
        store=store, code='BIG', amount=Decimal('8000.00'), min_order_amount=Decimal('0'),
        expires_at=timezone.now() + timedelta(days=1))
    collection = CouponCollection.objects.create(coupon=coupon, user=buyer)

    client.force_authenticate(user=buyer)
    response = client.post('/api/reservations/create_reservation/', {
        'item_id': item.id, 'quantity': 1, 'coupon_collection_id': collection.id,
    }, format='json')
    assert response.status_code == 201, response.data
    reservation = Reservation.objects.get(buyer=buyer)
    assert reservation.total_price == Decimal('2000.00')
    assert reservation.deposit_amount == Decimal('2000.00'), 'not the 5,000 sized on the full price'


# ---------------------------------------------------------------------------
# Represented sales: stock taken once, and given back when nothing is sold
# ---------------------------------------------------------------------------

def test_represented_sale_takes_stock_once_and_returns_it_on_expiry():
    """The representative flow takes stock when the reservation is *created*
    (unlike a normal sale). Completing it then took the unit again, and an
    expired one never gave it back — so every represented sale cost the
    seller two units, and every abandoned one cost them one."""
    from core.tasks import process_expired_reservations
    from core.models import Transaction
    seller, rep = make_user('aud_rp_s'), make_user('aud_rp_r')
    buyer, other_buyer = make_user('aud_rp_b', balance='1000'), make_user('aud_rp_b2')
    item = make_listing(seller)
    variant = item.default_variant
    variant.quantity = 2; variant.save()

    def represented(buyer, **extra):
        reservation = make_reservation(item, buyer, representative=rep,
                                       commission_amount=Decimal('5.00'), **extra)
        variant.take_stock(1)  # what create_representative_reservation does
        Transaction.objects.create(user=rep, transaction_type='commission', amount=Decimal('5'),
                                   status='pending', reservation=reservation, item=item)
        return reservation

    sold = represented(buyer)
    abandoned = represented(other_buyer, expires_at=timezone.now() - timedelta(minutes=1))
    variant.refresh_from_db()
    assert variant.quantity == 0

    complete_reservation(sold, 'full')
    variant.refresh_from_db()
    assert variant.quantity == 0, 'completion must not take the unit a second time'

    process_expired_reservations()
    variant.refresh_from_db(); abandoned.refresh_from_db()
    assert abandoned.status == 'expired'
    assert variant.quantity == 1, 'the abandoned unit is back on the shelf'
    assert Transaction.objects.get(reservation=abandoned, transaction_type='commission').status == 'cancelled'
    assert Transaction.objects.get(reservation=sold, transaction_type='commission').status == 'pending'


# ---------------------------------------------------------------------------
# Cancel after release: not a thing. Once the seller has been paid the sale
# is settled for everyone; problems from here are disputes.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('who', ['buyer', 'seller'])
def test_nobody_can_cancel_once_payment_was_released(client, who):
    seller, rep = make_user('aud_nc_s_' + who), make_user('aud_nc_r_' + who)
    buyer = make_user('aud_nc_b_' + who, balance='1000')
    reservation = make_reservation(make_listing(seller), buyer, representative=rep,
                                   commission_amount=Decimal('20.00'))
    complete_reservation(reservation, 'full')
    release_reservation_payment(reservation)
    # Force the one state the guard must catch even if a flow ever left it.
    Reservation.objects.filter(pk=reservation.pk).update(
        status='completed', seller_cancellation_approved=True)

    client.force_authenticate(user=buyer if who == 'buyer' else seller)
    response = client.post(f'/api/reservations/{reservation.id}/cancel/')
    assert response.status_code == 400, response.data
    assert 'تحرير' in response.data['message']
    assert balance(seller) == Decimal('80') and balance(rep) == Decimal('20')
    assert balance(buyer) == Decimal('900')
    assert Reservation.objects.get(pk=reservation.pk).status == 'completed'
