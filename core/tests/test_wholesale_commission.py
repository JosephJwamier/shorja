"""عمولة المنصة على البيع بالجملة.

كان البيع بالجملة الاستثناء الوحيد من العمولة: يأخذ البائع الثمن كاملاً عند
الموافقة. صار يخضع للقاعدة نفسها التي على كل بيع — نسبة التصنيف مضروبة في
شارة البائع — تُقتطع من حصّة البائع، والمشتري يدفع الثمن المكتوب كما هو.
"""
from decimal import Decimal

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, ItemTransfer, StoreBadge, Transaction,
    User, UserStore, get_or_create_wallet,
)
from core.services.commission import platform_account

pytestmark = pytest.mark.django_db

PRICE = Decimal('100000')


@pytest.fixture
def owner():
    return User.objects.create_user(username='wh_owner', password='pass12345')


@pytest.fixture
def reseller():
    user = User.objects.create_user(username='wh_reseller', password='pass12345')
    get_or_create_wallet(user).credit(Decimal('1000000'))
    return user


def make_transfer(owner, reseller, category, quantity=2):
    item = Item.objects.create(
        owner=owner, title='t', description='d', category=category,
        condition='new', quantity=10, status='active',
    )
    FixedPriceListing.objects.create(item=item, price=PRICE, deposit_percentage=10)
    return ItemTransfer.objects.create(
        item=item, from_user=owner, to_user=reseller, quantity=quantity)


def approve(owner, transfer):
    # A wholesale store is paid at approval; what any other seller is paid, and
    # when, is test_wholesale_money.py.
    store, _ = UserStore.objects.get_or_create(user=owner, defaults={'name': 'متجر'})
    StoreBadge.objects.create(store=store, kind=StoreBadge.KIND_WHOLESALE)
    owner = User.objects.get(pk=owner.pk)
    client = APIClient()
    client.force_authenticate(user=owner)
    return client.post(f'/api/transfers/{transfer.id}/approve/')


def balance(user):
    return get_or_create_wallet(user).balance


@override_settings(PLATFORM_COMMISSION_PERCENT='3')
def test_the_owner_is_paid_the_price_less_the_commission(owner, reseller):
    category = Category.objects.create(name='whcat', slug='whcat')
    transfer = make_transfer(owner, reseller, category)

    res = approve(owner, transfer)
    assert res.status_code == 200

    # 2 × 100,000 = 200,000؛ العمولة 3% = 6,000.
    assert balance(reseller) == Decimal('800000')
    assert balance(owner) == Decimal('194000')
    assert balance(platform_account()) == Decimal('6000')

    transfer.refresh_from_db()
    assert transfer.platform_commission_percent == Decimal('3.00')
    assert transfer.platform_commission_amount == Decimal('6000.00')
    assert res.data['data']['owner_net_amount'] == 194000.0
    assert Transaction.objects.get(
        transfer=transfer, transaction_type='commission').amount == Decimal('6000')


def test_the_category_rate_applies(owner, reseller):
    category = Category.objects.create(name='whcat2', slug='whcat2',
                                       commission_percent=Decimal('5'))
    transfer = make_transfer(owner, reseller, category, quantity=1)
    approve(owner, transfer)
    assert balance(owner) == Decimal('95000')
    assert balance(platform_account()) == Decimal('5000')


@override_settings(PLATFORM_COMMISSION_PERCENT='3')
def test_a_founder_badge_pays_no_commission(owner, reseller):
    store = UserStore.objects.create(user=owner, name='متجر', is_active=True)
    StoreBadge.objects.create(store=store, kind=StoreBadge.KIND_FOUNDER)
    category = Category.objects.create(name='whcat3', slug='whcat3')
    transfer = make_transfer(owner, reseller, category, quantity=1)

    approve(owner, transfer)
    assert balance(owner) == PRICE
    assert not Transaction.objects.filter(
        transfer=transfer, transaction_type='commission').exists()


@override_settings(PLATFORM_COMMISSION_PERCENT='3')
def test_a_failed_approval_takes_no_commission(owner, reseller):
    category = Category.objects.create(name='whcat4', slug='whcat4')
    transfer = make_transfer(owner, reseller, category, quantity=50)  # stock is 10

    res = approve(owner, transfer)
    assert res.status_code == 400
    assert balance(owner) == 0
    assert balance(platform_account()) == 0
