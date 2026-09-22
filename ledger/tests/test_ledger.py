"""The merchant's book: what it records, what it refuses, and how it stays
in step with in-app sales and stock."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, ItemVariant, Reservation, User, UserStore,
)
from core.wallet_utils import complete_reservation, get_or_create_wallet
from ledger.models import Contact, LedgerEntry
from ledger.services import record_app_sale, summarize, void_app_sale

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_merchant(username='shop'):
    user = User.objects.create_user(username=username, password='pass12345')
    store = UserStore.objects.create(user=user, name=f'{username} store')
    return user, store


def make_item(owner, quantity=10, price='100.00', cost=None, **extra):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(
        owner=owner, title='shirt', description='d', category=category,
        condition='new', quantity=quantity, status='active', **extra,
    )
    FixedPriceListing.objects.create(item=item, price=Decimal(price), deposit_percentage=10)
    if cost is not None:
        v = item.default_variant
        v.cost_price = Decimal(cost)
        v.save(update_fields=['cost_price'])
    return item


@pytest.fixture
def merchant():
    return make_merchant()


@pytest.fixture
def client(merchant):
    user, _ = merchant
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def post_entry(client, **body):
    return client.post('/api/ledger/entries/', body, format='json')


# ---------------------------------------------------------------------------
# contacts & balances
# ---------------------------------------------------------------------------

def test_customer_balance_is_credit_sales_minus_payments(client, merchant):
    _, store = merchant
    r = client.post('/api/ledger/contacts/', {'name': 'أبو علي', 'phone': '0770'}, format='json')
    assert r.status_code == 201, r.data
    cid = r.data['id']
    assert post_entry(client, kind='sale_credit', amount='50000', contact=cid).status_code == 201
    assert post_entry(client, kind='sale_credit', amount='25000', contact=cid).status_code == 201
    assert post_entry(client, kind='payment_in', amount='30000', contact=cid).status_code == 201

    r = client.get(f'/api/ledger/contacts/{cid}/')
    assert Decimal(r.data['balance']) == Decimal('45000.00')

    r = client.get(f'/api/ledger/contacts/{cid}/statement/')
    running = [Decimal(row['running_balance']) for row in r.data['rows']]
    assert running == [Decimal('50000.00'), Decimal('75000.00'), Decimal('45000.00')]
    assert Decimal(r.data['balance']) == Decimal('45000.00')


def test_supplier_balance_is_purchases_minus_payments(client):
    cid = client.post('/api/ledger/contacts/', {'name': 'مخزن الجملة', 'kind': 'supplier'},
                      format='json').data['id']
    assert post_entry(client, kind='purchase', amount='400000', contact=cid).status_code == 201
    assert post_entry(client, kind='payment_out', amount='150000', contact=cid).status_code == 201
    r = client.get('/api/ledger/contacts/?ordering=balance')
    assert Decimal(r.data[0]['balance']) == Decimal('250000.00')


def test_entry_kind_must_match_contact_kind(client):
    supplier = client.post('/api/ledger/contacts/', {'name': 's', 'kind': 'supplier'},
                           format='json').data['id']
    r = post_entry(client, kind='sale_credit', amount='10', contact=supplier)
    assert r.status_code == 400 and 'contact' in r.data
    r = post_entry(client, kind='sale_credit', amount='10')
    assert r.status_code == 400 and 'contact' in r.data
    r = post_entry(client, kind='expense', amount='10', contact=supplier)
    assert r.status_code == 400 and 'contact' in r.data


def test_contacts_are_private_to_the_store(client):
    other, other_store = make_merchant('other')
    theirs = Contact.objects.create(store=other_store, name='x')
    assert client.get(f'/api/ledger/contacts/{theirs.id}/').status_code == 404
    r = post_entry(client, kind='sale_credit', amount='10', contact=theirs.id)
    assert r.status_code == 400


def test_a_contact_with_history_is_archived_not_deleted(client):
    cid = client.post('/api/ledger/contacts/', {'name': 'a'}, format='json').data['id']
    post_entry(client, kind='sale_credit', amount='10', contact=cid)
    r = client.delete(f'/api/ledger/contacts/{cid}/')
    assert r.status_code == 200 and r.data['archived'] is True
    assert Contact.objects.get(id=cid).is_archived
    assert client.get('/api/ledger/contacts/').data == []
    assert len(client.get('/api/ledger/contacts/?archived=1').data) == 1


def test_users_without_a_store_are_turned_away():
    user = User.objects.create_user(username='nostore', password='pass12345')
    c = APIClient()
    c.force_authenticate(user=user)
    assert c.get('/api/ledger/dashboard/').status_code == 403
    assert c.get('/api/ledger/entries/').status_code == 403


# ---------------------------------------------------------------------------
# entries: validation, stock link, void
# ---------------------------------------------------------------------------

def test_app_sales_cannot_be_written_by_hand(client):
    r = post_entry(client, kind='sale_app', amount='10')
    assert r.status_code == 400 and 'kind' in r.data


def test_amount_must_be_positive(client):
    assert post_entry(client, kind='sale_cash', amount='0').status_code == 400
    assert post_entry(client, kind='sale_cash', amount='-5').status_code == 400


def test_a_shop_sale_linked_to_stock_takes_it_and_freezes_the_cost(client, merchant):
    user, store = merchant
    item = make_item(user, quantity=5, cost='60.00')
    variant = item.default_variant
    r = post_entry(client, kind='sale_cash', amount='300', item=item.id, quantity=2)
    assert r.status_code == 201, r.data
    variant.refresh_from_db(); item.refresh_from_db()
    assert variant.quantity == 3 and item.quantity == 3
    entry = LedgerEntry.objects.get(id=r.data['id'])
    assert entry.variant_id == variant.id
    assert entry.cost_amount == Decimal('120.00')

    # Voiding hands the stock back.
    r = client.post(f'/api/ledger/entries/{entry.id}/void/', {'reason': 'خطأ'}, format='json')
    assert r.status_code == 200 and r.data['is_void'] is True
    variant.refresh_from_db()
    assert variant.quantity == 5


def test_stock_link_refuses_more_than_the_shelf_holds(client, merchant):
    user, _ = merchant
    item = make_item(user, quantity=1)
    r = post_entry(client, kind='sale_cash', amount='300', item=item.id, quantity=2)
    assert r.status_code == 400 and 'quantity' in r.data
    item.refresh_from_db()
    assert item.quantity == 1


def test_stock_link_only_for_own_items_and_only_on_sales(client, merchant):
    other, _ = make_merchant('other2')
    theirs = make_item(other)
    r = post_entry(client, kind='sale_cash', amount='1', item=theirs.id, quantity=1)
    assert r.status_code == 400 and 'item' in r.data
    user, _ = merchant
    mine = make_item(user)
    r = post_entry(client, kind='expense', amount='1', item=mine.id, quantity=1)
    assert r.status_code == 400 and 'item' in r.data


def test_money_fields_are_frozen_after_entry(client):
    eid = post_entry(client, kind='sale_cash', amount='100', note='n').data['id']
    r = client.patch(f'/api/ledger/entries/{eid}/', {'amount': '200'}, format='json')
    assert r.status_code == 400
    r = client.patch(f'/api/ledger/entries/{eid}/', {'note': 'fixed'}, format='json')
    assert r.status_code == 200 and r.data['note'] == 'fixed'
    assert client.delete(f'/api/ledger/entries/{eid}/').status_code == 405


def test_void_hides_the_entry_from_the_book_but_keeps_the_row(client):
    eid = post_entry(client, kind='sale_cash', amount='100').data['id']
    client.post(f'/api/ledger/entries/{eid}/void/', {'reason': 'مكرّر'}, format='json')
    assert client.get('/api/ledger/entries/').data['results'] == []
    rows = client.get('/api/ledger/entries/?include_void=1').data['results']
    assert len(rows) == 1 and rows[0]['void_reason'] == 'مكرّر'
    assert LedgerEntry.objects.filter(id=eid).exists()


# ---------------------------------------------------------------------------
# mirroring in-app sales
# ---------------------------------------------------------------------------

def make_completed_sale(seller, buyer, price='120000.00', quantity=1, cost=None):
    item = make_item(seller, quantity=5, price=price, cost=cost)
    get_or_create_wallet(buyer).credit(Decimal('1000000.00'))
    reservation = Reservation.objects.create(
        item=item, buyer=buyer, quantity=quantity,
        total_price=Decimal(price) * quantity,
        deposit_amount=Decimal(price) * quantity / 10,
        status='pending', expires_at=timezone.now() + timedelta(days=1),
    )
    from django.db import transaction
    with transaction.atomic():
        complete_reservation(reservation, 'full')
    return reservation


def test_a_completed_reservation_writes_itself_into_the_book(merchant):
    seller, store = merchant
    buyer = User.objects.create_user(username='buyer', password='pass12345')
    reservation = make_completed_sale(seller, buyer, cost='80000.00')
    entry = reservation.ledger_entry
    assert entry.kind == 'sale_app' and entry.source == 'app'
    assert entry.amount == Decimal('120000.00')
    assert entry.cost_amount == Decimal('80000.00')
    assert entry.store == store
    # Idempotent: completing twice would not double-count.
    assert record_app_sale(reservation) is None


def test_sellers_without_a_store_get_no_ledger_row():
    seller = User.objects.create_user(username='plain', password='pass12345')
    buyer = User.objects.create_user(username='buyer2', password='pass12345')
    reservation = make_completed_sale(seller, buyer)
    assert not hasattr(reservation, 'ledger_entry') or reservation.ledger_entry is None


def test_cancelling_a_completed_sale_voids_its_row(merchant):
    seller, _ = merchant
    buyer = User.objects.create_user(username='buyer3', password='pass12345')
    reservation = make_completed_sale(seller, buyer)
    void_app_sale(reservation, 'أُلغي الحجز')
    entry = LedgerEntry.objects.get(reservation=reservation)
    assert entry.is_void and entry.void_reason == 'أُلغي الحجز'


def test_app_rows_cannot_be_voided_from_the_ledger_api(client, merchant):
    seller, _ = merchant
    buyer = User.objects.create_user(username='buyer4', password='pass12345')
    reservation = make_completed_sale(seller, buyer)
    r = client.post(f'/api/ledger/entries/{reservation.ledger_entry.id}/void/', {}, format='json')
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# summary & dashboard
# ---------------------------------------------------------------------------

def test_summary_adds_up_the_period(client, merchant):
    user, store = merchant
    cid = client.post('/api/ledger/contacts/', {'name': 'c'}, format='json').data['id']
    post_entry(client, kind='sale_cash', amount='100000')
    post_entry(client, kind='sale_credit', amount='50000', contact=cid)
    post_entry(client, kind='payment_in', amount='20000', contact=cid)
    post_entry(client, kind='expense', amount='30000', expense_category='rent')
    post_entry(client, kind='expense', amount='5000')  # → 'other'
    # Last month: must not count.
    LedgerEntry.objects.create(store=store, kind='sale_cash', amount=Decimal('999999'),
                               occurred_at=timezone.now() - timedelta(days=40))

    r = client.get('/api/ledger/summary/?period=month')
    assert r.status_code == 200, r.data
    d = r.data
    assert Decimal(d['revenue']) == Decimal('150000.00')
    assert Decimal(d['sales_cash']) == Decimal('100000.00')
    assert Decimal(d['sales_credit']) == Decimal('50000.00')
    assert Decimal(d['expenses']) == Decimal('35000.00')
    assert Decimal(d['net']) == Decimal('115000.00')
    assert Decimal(d['cash_in']) == Decimal('120000.00')
    assert Decimal(d['cash_out']) == Decimal('35000.00')
    assert Decimal(d['receivables']) == Decimal('30000.00')
    assert d['gross_profit'] is None  # no cost known anywhere
    cats = {c['expense_category']: Decimal(c['total']) for c in d['expenses_by_category']}
    assert cats == {'rent': Decimal('30000.00'), 'other': Decimal('5000.00')}
    assert len(d['daily']) == 1


def test_gross_profit_only_covers_costed_sales(client, merchant):
    user, store = merchant
    item = make_item(user, quantity=5, cost='40')
    post_entry(client, kind='sale_cash', amount='100', item=item.id, quantity=1)
    post_entry(client, kind='sale_cash', amount='500')  # no cost known
    d = client.get('/api/ledger/summary/?period=today').data
    assert Decimal(d['revenue']) == Decimal('600.00')
    assert Decimal(d['costed_revenue']) == Decimal('100.00')
    assert Decimal(d['gross_profit']) == Decimal('60.00')


def test_dashboard_bundles_book_and_store_state(client, merchant):
    user, store = merchant
    low = make_item(user, quantity=2)
    make_item(user, quantity=50)
    cid = client.post('/api/ledger/contacts/', {'name': 'مدين'}, format='json').data['id']
    post_entry(client, kind='sale_credit', amount='70000', contact=cid)
    post_entry(client, kind='sale_cash', amount='10000', item=low.id, quantity=1)

    r = client.get('/api/ledger/dashboard/?period=month')
    assert r.status_code == 200, r.data
    d = r.data
    assert Decimal(d['summary']['revenue']) == Decimal('80000.00')
    assert Decimal(d['balances']['receivables']) == Decimal('70000.00')
    assert d['balances']['top_debtors'][0]['name'] == 'مدين'
    assert d['stock']['active_items'] == 2
    assert [v['item_id'] for v in d['stock']['low_stock']] == [low.id]
    assert d['top_items'][0]['item_id'] == low.id
    assert d['period']['key'] == 'month'
    assert 'previous' in d


def test_custom_period_needs_both_dates(client):
    assert client.get('/api/ledger/summary/?period=custom&start=2026-01-01').status_code == 400
    r = client.get('/api/ledger/summary/?period=custom&start=2026-01-01&end=2026-01-31')
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# cost price stays private
# ---------------------------------------------------------------------------

def test_cost_price_is_shown_to_the_owner_only(merchant):
    user, _ = merchant
    item = make_item(user, cost='55.00')
    owner = APIClient(); owner.force_authenticate(user=user)
    data = owner.get(f'/api/items/{item.id}/').data
    data = data.get('data', data)
    assert data['variants'][0]['cost_price'] == '55.00'

    stranger = User.objects.create_user(username='stranger', password='pass12345')
    other = APIClient(); other.force_authenticate(user=stranger)
    data = other.get(f'/api/items/{item.id}/').data
    data = data.get('data', data)
    assert 'cost_price' not in data['variants'][0]


def test_cost_price_round_trips_through_item_create(merchant):
    user, _ = merchant
    c = APIClient(); c.force_authenticate(user=user)
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    r = c.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': category.id,
        'condition': 'new', 'quantity': 0,
        'options': [{'name': 'المقاس', 'values': ['M']}],
        'variants': [{'attributes': {'المقاس': 'M'}, 'quantity': 3, 'cost_price': '12.50'}],
    }, format='json')
    assert r.status_code == 201, r.data
    item_id = r.data.get('data', r.data)['id']
    assert ItemVariant.objects.get(item_id=item_id).cost_price == Decimal('12.50')
