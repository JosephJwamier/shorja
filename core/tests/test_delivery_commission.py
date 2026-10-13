"""
The platform's cut of a delivery company's fee, and the COD cash netting it
depends on.

The rule being protected here is that the company is paid its share exactly
once, by exactly one of two routes:

  prepaid  the buyer paid the fee in-app, the platform holds it and credits
           the company's wallet at release;
  COD      the company kept its share out of the cash it collected and
           handed the platform the rest — so its wallet must NOT be
           credited again.

Before this feature the COD path credited the wallet *and* expected the
whole collected sum back, which paid the company twice. No test covered
cash collection at all, which is why that went unnoticed.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.delivery_utils import create_outbound_delivery_order
from core.models import (
    Category, DeliveryCompany, DeliveryOrder, FixedPriceListing, Item, Location,
    Reservation, User, get_or_create_wallet,
)
from core.services.commission import delivery_commission_for, platform_account
from core.wallet_utils import release_reservation_payment

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_seller_commission(settings):
    """Keep the seller's commission out of the arithmetic — it is covered in
    test_commission_and_delivery_rates and would only obscure the courier
    numbers here."""
    settings.PLATFORM_COMMISSION_PERCENT = '0'


def make_user(username, balance='0'):
    user = User.objects.create_user(username=username, password='x')
    if Decimal(balance) > 0:
        get_or_create_wallet(user).credit(Decimal(balance))
    return user


def make_company(percent='10', account_name='courier'):
    return DeliveryCompany.objects.create(
        name='c', is_active=True, account=make_user(account_name),
        intra_governorate_fee=Decimal('10000'), inter_governorate_fee=Decimal('15000'),
        platform_commission_percent=Decimal(percent),
    )


def make_location(user, state='بغداد'):
    return Location.objects.create(user=user, name='home', address_line1='x', city='c',
                                   state=state, country='Iraq', contact_person='p',
                                   contact_phone='1', is_primary=True)


def make_reservation(seller, buyer, *, deposit_only, fee='10000',
                     total='100000', deposit='20000'):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=seller, title='t', description='d', category=category,
                               condition='new', quantity=1, status='active')
    FixedPriceListing.objects.create(item=item, price=Decimal(total), deposit_percentage=20)
    make_location(seller)
    reservation = Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal(deposit) if deposit_only else Decimal('0'),
        total_price=Decimal(total), status='completed',
        is_deposit_only=deposit_only, completed_at=timezone.now(),
        delivery_fee=Decimal(fee),
        expires_at=timezone.now() + timedelta(days=1),
        delivery_location=make_location(buyer),
    )
    return reservation


def balance(user):
    return get_or_create_wallet(user).balance


def collect_cash(order):
    order.cash_collected = True
    order.cash_collected_at = timezone.now()
    order.save(update_fields=['cash_collected', 'cash_collected_at'])


# ---------------------------------------------------------------------------
# Rate resolution
# ---------------------------------------------------------------------------

def test_rate_is_per_company_with_no_platform_wide_fallback():
    negotiated = make_company(percent='10', account_name='c1')
    never_negotiated = DeliveryCompany.objects.create(name='c2', is_active=True)
    assert delivery_commission_for(negotiated, Decimal('10000')) == (Decimal('10.00'), Decimal('1000.00'))
    # A company whose terms were never set keeps its whole fee.
    assert delivery_commission_for(never_negotiated, Decimal('10000')) == (Decimal('0.00'), Decimal('0.00'))
    assert delivery_commission_for(None, Decimal('10000')) == (Decimal('0.00'), Decimal('0.00'))
    assert delivery_commission_for(negotiated, Decimal('0')) == (Decimal('0.00'), Decimal('0.00'))


def test_rate_is_frozen_on_the_order_when_the_trip_is_dispatched():
    company = make_company(percent='10')
    seller, buyer = make_user('fz_s'), make_user('fz_b')
    reservation = make_reservation(seller, buyer, deposit_only=False)
    order = create_outbound_delivery_order(reservation)
    assert order.platform_commission_percent == Decimal('10.00')
    assert order.platform_commission_amount == Decimal('1000.00')

    company.platform_commission_percent = Decimal('50')
    company.save()
    order.refresh_from_db()
    assert order.platform_commission_amount == Decimal('1000.00'), 'a dispatched trip is not re-priced'


# ---------------------------------------------------------------------------
# Prepaid: the platform holds the fee and splits it
# ---------------------------------------------------------------------------

def test_prepaid_splits_the_fee_between_company_and_platform():
    company = make_company(percent='10')
    seller, buyer = make_user('pp_s'), make_user('pp_b')
    reservation = make_reservation(seller, buyer, deposit_only=False)
    create_outbound_delivery_order(reservation)

    release_reservation_payment(reservation)

    assert balance(company.account) == Decimal('9000'), 'fee minus the platform cut'
    assert balance(platform_account()) == Decimal('1000')
    assert balance(seller) == Decimal('100000'), 'the courier fee never touches the seller'


def test_zero_rate_leaves_the_company_with_the_whole_fee():
    company = make_company(percent='0')
    seller, buyer = make_user('zr_s'), make_user('zr_b')
    reservation = make_reservation(seller, buyer, deposit_only=False)
    create_outbound_delivery_order(reservation)

    release_reservation_payment(reservation)

    assert balance(company.account) == Decimal('10000')
    assert balance(platform_account()) == Decimal('0')


# ---------------------------------------------------------------------------
# COD: the company nets its share out of the cash it collected
# ---------------------------------------------------------------------------

def test_cod_cash_due_is_the_remainder_plus_the_platform_cut():
    make_company(percent='10')
    seller, buyer = make_user('cd_s'), make_user('cd_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    order = create_outbound_delivery_order(reservation)

    # Buyer hands over remainder (80000) + fee (10000).
    assert order.cash_to_collect == Decimal('90000')
    # Company keeps 10000 - 1000; the platform is owed the rest.
    assert order.cash_due_to_platform == Decimal('81000')


def test_cod_does_not_credit_the_company_a_second_time():
    company = make_company(percent='10')
    seller, buyer = make_user('c2_s'), make_user('c2_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    order = create_outbound_delivery_order(reservation)
    collect_cash(order)

    release_reservation_payment(reservation)

    assert balance(company.account) == Decimal('0'), 'its share was kept in cash, not paid again'
    assert balance(platform_account()) == Decimal('1000'), 'the cut arrived inside the settlement'
    assert balance(seller) == Decimal('100000'), 'seller still gets the full item price'


def test_cod_liability_counts_the_net_not_the_gross():
    company = make_company(percent='10')
    seller, buyer = make_user('lb_s'), make_user('lb_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    order = create_outbound_delivery_order(reservation)
    collect_cash(order)

    # 81000, not the 90000 the buyer handed over: the company legitimately
    # keeps 9000 of it, and chasing that forever was the old bug.
    assert company.outstanding_cash_liability() == Decimal('81000')


def test_settled_cash_leaves_the_outstanding_liability():
    from core.models import CashSettlement
    company = make_company(percent='10')
    seller, buyer = make_user('st_s'), make_user('st_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    order = create_outbound_delivery_order(reservation)
    collect_cash(order)

    settlement = CashSettlement.objects.create(company=company, amount=Decimal('81000'))
    DeliveryOrder.objects.filter(pk=order.pk).update(cash_settlement=settlement)

    assert company.outstanding_cash_liability() == Decimal('0')


def test_uncollected_cod_owes_nothing_yet():
    company = make_company(percent='10')
    seller, buyer = make_user('uc_s'), make_user('uc_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    create_outbound_delivery_order(reservation)   # cash not collected

    assert company.outstanding_cash_liability() == Decimal('0')


def test_money_in_equals_money_out_on_a_cod_sale():
    """The invariant the double-payment bug broke: everything the platform
    physically received is paid out, and nothing more."""
    company = make_company(percent='10')
    seller, buyer = make_user('mi_s'), make_user('mi_b')
    reservation = make_reservation(seller, buyer, deposit_only=True,
                                   total='100000', deposit='20000', fee='10000')
    order = create_outbound_delivery_order(reservation)
    collect_cash(order)

    release_reservation_payment(reservation)

    received = Decimal('20000') + order.cash_due_to_platform      # deposit + settlement
    paid_out = balance(seller) + balance(company.account) + balance(platform_account())
    assert received == paid_out == Decimal('101000')
