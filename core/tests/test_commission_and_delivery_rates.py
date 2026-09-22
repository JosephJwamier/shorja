"""Platform commission (per category, taken from the seller at release) and
the per-route/per-size delivery tariff."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.delivery_utils import quote_delivery_fee
from core.models import (
    Category, DeliveryCompany, DeliveryOrder, DeliveryRate, FixedPriceListing, Item, Location,
    Reservation, StoreBadge, Transaction, User, UserStore, get_or_create_wallet,
)
from core.services.commission import commission_for, platform_account
from core.services.store_badges import grant_badge
from core.wallet_utils import complete_reservation, release_reservation_payment
from ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def platform_default(settings):
    settings.PLATFORM_COMMISSION_PERCENT = '5'


def make_user(username, balance='0'):
    user = User.objects.create_user(username=username, password='x')
    if Decimal(balance) > 0:
        get_or_create_wallet(user).credit(Decimal(balance))
    return user


def make_categories():
    root = Category.objects.create(name='Electronics', slug='electronics', commission_percent=Decimal('8'))
    child = Category.objects.create(name='Phones', slug='phones', parent=root)             # inherits 8
    grandchild = Category.objects.create(name='Cases', slug='cases', parent=child,
                                         commission_percent=Decimal('12'))                   # own 12
    other = Category.objects.create(name='Misc', slug='misc')                                 # default 5
    return root, child, grandchild, other


def make_item(owner, category, price='100000.00', package_size='medium'):
    item = Item.objects.create(owner=owner, title='t', description='d', category=category,
                               condition='new', quantity=5, status='active', package_size=package_size)
    FixedPriceListing.objects.create(item=item, price=Decimal(price), deposit_percentage=10)
    return item


def make_location(user, state='بغداد'):
    return Location.objects.create(user=user, name='home', address_line1='x', city='c', state=state,
                                   country='Iraq', contact_person='p', contact_phone='1')


def balance(user):
    return get_or_create_wallet(user).balance


# ---------------------------------------------------------------------------
# Rate resolution
# ---------------------------------------------------------------------------

def test_category_rate_inherits_up_the_tree_then_platform_default():
    root, child, grandchild, other = make_categories()
    assert root.effective_commission_percent() == Decimal('8')
    assert child.effective_commission_percent() == Decimal('8')
    assert grandchild.effective_commission_percent() == Decimal('12')
    assert other.effective_commission_percent() == Decimal('5')


def test_badge_multiplier_founder_zero_pioneer_half():
    root, child, _, _ = make_categories()
    seller, admin = make_user('cm_s'), User.objects.create_superuser('cm_root', 'r@x.io', 'x')
    store = UserStore.objects.create(user=seller, name='s')
    item = make_item(seller, child, price='100000')
    assert commission_for(item, Decimal('100000')) == (Decimal('8.00'), Decimal('8000.00'))
    grant_badge(store, StoreBadge.KIND_PIONEER, by=admin)
    assert commission_for(item, Decimal('100000')) == (Decimal('4.00'), Decimal('4000.00'))
    grant_badge(store, StoreBadge.KIND_FOUNDER, by=admin)
    assert commission_for(item, Decimal('100000')) == (Decimal('0.00'), Decimal('0.00'))


# ---------------------------------------------------------------------------
# Frozen at creation, taken at release — from the seller only
# ---------------------------------------------------------------------------

def test_commission_is_frozen_on_the_reservation_at_creation(client):
    _, child, _, _ = make_categories()
    seller, buyer = make_user('fz_s'), make_user('fz_b', balance='500000')
    item = make_item(seller, child, price='100000')
    client.force_authenticate(user=buyer)
    response = client.post('/api/reservations/create_reservation/', {
        'item_id': item.id, 'quantity': 2, 'delivery_location_id': make_location(buyer).id,
    }, format='json')
    assert response.status_code == 201, response.data
    r = Reservation.objects.get(buyer=buyer)
    assert r.platform_commission_percent == Decimal('8.00')
    assert r.platform_commission_amount == Decimal('16000.00')
    assert response.data['data']['seller_net'] == '184000.00'
    # A later rate change does not touch it.
    child.parent.commission_percent = Decimal('20'); child.parent.save()
    r.refresh_from_db()
    assert r.platform_commission_amount == Decimal('16000.00')


def test_release_credits_the_platform_and_only_the_seller_pays():
    _, child, _, _ = make_categories()
    seller, buyer = make_user('rl_s'), make_user('rl_b', balance='500000')
    item = make_item(seller, child, price='100000')
    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=make_location(buyer),
        platform_commission_percent=Decimal('8'), platform_commission_amount=Decimal('8000'),
    )
    complete_reservation(r, 'full')
    assert balance(buyer) == Decimal('400000'), 'buyer pays the listed price, nothing more'
    release_reservation_payment(r)
    assert balance(seller) == Decimal('92000')
    assert balance(platform_account()) == Decimal('8000')
    txn = Transaction.objects.get(user=platform_account(), reservation=r)
    assert txn.transaction_type == 'commission' and txn.status == 'completed'


def test_release_with_representative_takes_platform_cut_before_the_split():
    _, child, _, _ = make_categories()
    seller, rep, buyer = make_user('rp_s'), make_user('rp_r'), make_user('rp_b', balance='500000')
    item = make_item(seller, child, price='100000')
    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=make_location(buyer),
        representative=rep, commission_amount=Decimal('10000'),
        platform_commission_percent=Decimal('8'), platform_commission_amount=Decimal('8000'),
    )
    complete_reservation(r, 'full')
    release_reservation_payment(r)
    assert balance(platform_account()) == Decimal('8000')
    assert balance(rep) == Decimal('10000'), 'the representative gets their full agreed cut'
    assert balance(seller) == Decimal('82000')


def test_partial_refund_scales_the_platform_cut():
    _, child, _, _ = make_categories()
    seller, buyer = make_user('pr_s'), make_user('pr_b', balance='500000')
    item = make_item(seller, child, price='100000')
    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=make_location(buyer),
        platform_commission_percent=Decimal('8'), platform_commission_amount=Decimal('8000'),
    )
    complete_reservation(r, 'full')
    # Half goes back to the buyer (dispute); only half of the sale is commissioned.
    release_reservation_payment(r, amount=Decimal('50000'))
    assert balance(platform_account()) == Decimal('4000')
    assert balance(seller) == Decimal('46000')


def test_commission_lands_in_the_sellers_ledger_as_an_expense():
    _, child, _, _ = make_categories()
    seller, buyer = make_user('lg_s'), make_user('lg_b', balance='500000')
    UserStore.objects.create(user=seller, name='s')
    item = make_item(seller, child, price='100000')
    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=make_location(buyer),
        platform_commission_percent=Decimal('8'), platform_commission_amount=Decimal('8000'),
    )
    complete_reservation(r, 'full')
    release_reservation_payment(r)
    expense = LedgerEntry.objects.get(kind='expense', expense_category='commission')
    assert expense.amount == Decimal('8000') and expense.source == 'app'
    # Releasing again (guarded) must not book it twice.
    release_reservation_payment(Reservation.objects.get(pk=r.pk))
    assert LedgerEntry.objects.filter(expense_category='commission').count() == 1


def test_zero_rate_means_no_transaction_and_no_platform_row(settings):
    settings.PLATFORM_COMMISSION_PERCENT = '0'
    _, _, _, other = make_categories()
    seller, buyer = make_user('z_s'), make_user('z_b', balance='500000')
    item = make_item(seller, other, price='100000')
    assert commission_for(item, Decimal('100000')) == (Decimal('0.00'), Decimal('0.00'))
    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=make_location(buyer),
    )
    complete_reservation(r, 'full')
    release_reservation_payment(r)
    assert balance(seller) == Decimal('100000')
    assert not Transaction.objects.filter(transaction_type='commission', reservation=r).exists()


# ---------------------------------------------------------------------------
# Delivery tariff
# ---------------------------------------------------------------------------

def make_company():
    return DeliveryCompany.objects.create(
        name='fast', intra_governorate_fee=Decimal('5000'), inter_governorate_fee=Decimal('10000'))


def test_most_specific_rate_wins_and_flat_fees_are_the_last_resort():
    co = make_company()
    DeliveryRate.objects.create(company=co, fee=Decimal('7000'))                                  # * * *
    DeliveryRate.objects.create(company=co, to_governorate='أربيل', fee=Decimal('12000'))         # * أربيل *
    DeliveryRate.objects.create(company=co, from_governorate='بغداد', to_governorate='أربيل',
                                fee=Decimal('11000'))                                             # بغداد أربيل *
    DeliveryRate.objects.create(company=co, from_governorate='بغداد', to_governorate='أربيل',
                                package_size='bulky', fee=Decimal('25000'))                       # exact
    DeliveryRate.objects.create(company=co, package_size='bulky', fee=Decimal('20000'))          # * * bulky

    assert quote_delivery_fee(co, 'بغداد', 'أربيل', 'bulky') == Decimal('25000')
    assert quote_delivery_fee(co, 'بغداد', 'أربيل', 'small') == Decimal('11000')
    assert quote_delivery_fee(co, 'البصرة', 'أربيل', 'small') == Decimal('12000')
    assert quote_delivery_fee(co, 'البصرة', 'نينوى', 'bulky') == Decimal('20000')
    assert quote_delivery_fee(co, 'البصرة', 'نينوى', 'small') == Decimal('7000')

    # No rows at all → the company's flat fees exactly as before.
    bare = DeliveryCompany.objects.create(name='bare', is_active=False,
                                          intra_governorate_fee=Decimal('5000'),
                                          inter_governorate_fee=Decimal('10000'))
    assert quote_delivery_fee(bare, 'بغداد', 'بغداد', 'small') == Decimal('5000')
    assert quote_delivery_fee(bare, 'بغداد', 'أربيل', 'small') == Decimal('10000')
    assert quote_delivery_fee(bare, '', 'أربيل', 'small') == Decimal('10000'), 'unknown origin → conservative'


def test_inactive_rate_rows_are_ignored():
    co = make_company()
    DeliveryRate.objects.create(company=co, fee=Decimal('1'), is_active=False)
    assert quote_delivery_fee(co, 'بغداد', 'بغداد', 'small') == Decimal('5000')


def test_quote_endpoint_matches_the_charge(client):
    co = make_company()
    DeliveryRate.objects.create(company=co, from_governorate='بغداد', to_governorate='البصرة',
                                package_size='large', fee=Decimal('15000'))
    _, child, _, _ = make_categories()
    seller, buyer = make_user('q_s'), make_user('q_b', balance='500000')
    make_location(seller, 'بغداد')
    item = make_item(seller, child, price='100000', package_size='large')
    dest = make_location(buyer, 'البصرة')

    client.force_authenticate(user=buyer)
    quote = client.get('/api/reservations/delivery-quote/', {'item_id': item.id, 'location_id': dest.id})
    assert quote.status_code == 200, quote.data
    assert quote.data['data']['fee'] == '15000.00'

    r = Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10000'), total_price=Decimal('100000'), status='pending',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=dest,
    )
    complete_reservation(r, 'full')
    assert r.delivery_fee == Decimal('15000'), 'what was quoted is what was charged'


def test_quote_endpoint_refuses_someone_elses_address(client):
    make_company()
    _, child, _, _ = make_categories()
    seller, buyer, other = make_user('qa_s'), make_user('qa_b'), make_user('qa_o')
    item = make_item(seller, child)
    dest = make_location(other)
    client.force_authenticate(user=buyer)
    assert client.get('/api/reservations/delivery-quote/',
                      {'item_id': item.id, 'location_id': dest.id}).status_code == 404
