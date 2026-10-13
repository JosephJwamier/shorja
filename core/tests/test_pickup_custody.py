"""
Where a delivery is collected from when ownership and custody disagree.

A wholesale transfer moves ownership with a click, but the goods stay in
the original seller's warehouse for the length of the agreement
(ItemTransfer: "The stock physically stays in the owner's warehouse"). So:

  while the term runs   address = the custodian's (first seller's) store,
                        contact = the reseller, who is who the platform and
                        the driver deal with; the custodian is named in the
                        order's notes so the driver can reach the warehouse.
  once the term ends    custody follows ownership — the reseller's own
                        address, same as an item never transferred.

Before this, every delivery call site asked only "who owns it?", which sent
drivers to the reseller's home and priced the trip from the wrong city.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.delivery_utils import (
    create_outbound_delivery_order, compute_delivery_fee, pickup_governorate, resolve_pickup,
)
from core.models import (
    Category, DeliveryCompany, FixedPriceListing, Item, ItemTransfer, Location,
    Reservation, User, UserStore, get_or_create_wallet,
)

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='x', phone='0770' + '0' * 6)


def make_location(user, state, primary=True):
    return Location.objects.create(
        user=user, name=state, address_line1=f'{state} st', city=state, state=state,
        country='Iraq', contact_person=f'{user.username}-contact', contact_phone='07701112233',
        is_primary=primary,
    )


def make_item(owner, category=None):
    category = category or Category.objects.get_or_create(name='cat', slug='cat')[0]
    item = Item.objects.create(owner=owner, current_holder=owner, title='t', description='d',
                               category=category, condition='new', quantity=10, status='active')
    FixedPriceListing.objects.create(item=item, price=Decimal('100000'), deposit_percentage=10)
    return item


def make_transfer(owner, reseller, item, *, days=30, status='active'):
    """The reseller's own item, linked back to a running agreement — the
    shape ItemTransfer.activate() leaves behind."""
    resulting = Item.objects.create(
        owner=reseller, current_holder=reseller, title=item.title, description='d',
        category=item.category, condition='new', quantity=2, status='active',
        current_location=item.current_location,
    )
    FixedPriceListing.objects.create(item=resulting, price=Decimal('120000'), deposit_percentage=10)
    now = timezone.now()
    ItemTransfer.objects.create(
        item=item, from_user=owner, to_user=reseller, status=status,
        quantity=2, wholesale_price_per_unit=Decimal('90000'), resulting_item=resulting,
        duration_days=days, agreement_start_date=now,
        agreement_end_date=now + timedelta(days=days), approved_at=now,
    )
    return resulting


# ---------------------------------------------------------------------------
# resolve_pickup
# ---------------------------------------------------------------------------

def test_goods_stay_with_the_custodian_while_the_term_runs():
    owner, reseller = make_user('cu_o'), make_user('cu_r')
    warehouse = make_location(owner, 'البصرة')
    make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))

    location, custodian, contact = resolve_pickup(resold)

    assert location == warehouse, 'collected from the warehouse, not the reseller'
    assert custodian == owner
    assert contact == reseller, 'the reseller stays the party we deal with'


def test_store_location_wins_over_a_personal_address():
    owner, reseller = make_user('sl_o'), make_user('sl_r')
    make_location(owner, 'بغداد')                       # personal, primary
    store_branch = make_location(owner, 'أربيل', primary=False)
    UserStore.objects.create(user=owner, name='s', default_location=store_branch)
    make_location(reseller, 'النجف')
    resold = make_transfer(owner, reseller, make_item(owner))

    location, _, _ = resolve_pickup(resold)

    assert location == store_branch, 'a declared store location is where goods ship from'


def test_custody_returns_to_the_reseller_once_the_term_ends():
    owner, reseller = make_user('te_o'), make_user('te_r')
    make_location(owner, 'البصرة')
    reseller_home = make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))

    transfer = resold.acquired_via_transfer.get()
    transfer.agreement_end_date = timezone.now() - timedelta(days=1)
    transfer.save(update_fields=['agreement_end_date'])

    location, custodian, contact = resolve_pickup(resold)
    assert location == reseller_home
    assert custodian is None
    assert contact == reseller


def test_open_ended_agreement_is_treated_as_already_ended():
    # term_has_ended() is True with no end date: nothing binds the goods to
    # the custodian, so the owner's own address applies.
    owner, reseller = make_user('oe_o'), make_user('oe_r')
    make_location(owner, 'البصرة')
    reseller_home = make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))
    transfer = resold.acquired_via_transfer.get()
    transfer.agreement_end_date = None
    transfer.save(update_fields=['agreement_end_date'])

    location, custodian, _ = resolve_pickup(resold)
    assert location == reseller_home and custodian is None


def test_completed_transfer_no_longer_holds_the_goods():
    owner, reseller = make_user('cp_o'), make_user('cp_r')
    make_location(owner, 'البصرة')
    reseller_home = make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner), status='completed')

    location, custodian, _ = resolve_pickup(resold)
    assert location == reseller_home and custodian is None


def test_untransferred_item_ships_from_its_owner():
    owner = make_user('pl_o')
    home = make_location(owner, 'بغداد')
    assert resolve_pickup(make_item(owner)) == (home, None, owner)


def test_custodian_without_an_address_falls_back_to_the_owner():
    # Dispatching to a custodian we have no address for would send the
    # driver nowhere; the reseller's address is at least reachable.
    owner, reseller = make_user('na_o'), make_user('na_r')
    reseller_home = make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))

    location, custodian, _ = resolve_pickup(resold)
    assert location == reseller_home and custodian is None


# ---------------------------------------------------------------------------
# What the delivery company actually receives
# ---------------------------------------------------------------------------

def make_reservation(item, buyer, buyer_state='البصرة'):
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('0'),
        total_price=Decimal('120000'), status='completed', completed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=1),
        delivery_location=make_location(buyer, buyer_state),
    )


def test_delivery_order_carries_the_warehouse_address_and_names_the_custodian():
    DeliveryCompany.objects.create(name='c', is_active=True, account=make_user('courier'),
                                   intra_governorate_fee=Decimal('5000'),
                                   inter_governorate_fee=Decimal('15000'))
    owner, reseller, buyer = make_user('do_o'), make_user('do_r'), make_user('do_b')
    warehouse = make_location(owner, 'البصرة')
    make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))

    order = create_outbound_delivery_order(make_reservation(resold, buyer))

    assert order.from_address == warehouse.get_full_address()
    assert order.from_governorate == 'البصرة'
    assert owner.username in order.special_notes, 'the driver is told whose gate to knock on'


def test_fee_is_priced_from_the_warehouse_not_the_resellers_city():
    DeliveryCompany.objects.create(name='c', is_active=True, account=make_user('courier2'),
                                   intra_governorate_fee=Decimal('5000'),
                                   inter_governorate_fee=Decimal('15000'))
    owner, reseller, buyer = make_user('fe_o'), make_user('fe_r'), make_user('fe_b')
    make_location(owner, 'البصرة')
    make_location(reseller, 'بغداد')
    resold = make_transfer(owner, reseller, make_item(owner))

    # Goods and buyer are both in البصرة; only the reseller sits in بغداد.
    reservation = make_reservation(resold, buyer, buyer_state='البصرة')

    assert pickup_governorate(resold) == 'البصرة'
    assert compute_delivery_fee(reservation) == Decimal('5000'), 'same-governorate rate'
