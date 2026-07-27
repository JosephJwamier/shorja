"""
Phase 4 — dashboard analytics regression tests.

Aggregation bugs are invisible until someone eyeballs a number that looks
wrong on the dashboard, so every figure here gets a test that builds a
known scenario and checks the exact number, not just "it returns something".
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from core.iraq_governorates import IRAQ_GOVERNORATES
from core.models import (
    Category, Dispute, Item, LiveSession, Location, Reservation, Transaction,
    User, UserStore, WithdrawalRequest, get_or_create_wallet,
)
from core.services import analytics
from core.services.support import SUPPORT_USERNAME

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def backdate_user(user, when):
    User.objects.filter(pk=user.pk).update(created_at=when)


def make_category():
    category, _ = Category.objects.get_or_create(name='cat', slug='cat', defaults={'name_ar': 'فئة'})
    return category


def make_reservation(*, owner, buyer, total_price, status, completed_at, category=None, delivery_location=None):
    item = Item.objects.create(
        owner=owner, title='item', description='d', category=category or make_category(),
        condition='good', quantity=1, status='active',
    )
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('1'), total_price=total_price,
        status=status, expires_at=timezone.now() + timedelta(days=1),
        completed_at=completed_at, is_deposit_only=False, delivery_location=delivery_location,
    )


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# registrations
# ---------------------------------------------------------------------------

def test_registration_counts_bucket_by_today_week_month():
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    u_today = make_user('reg_today')
    backdate_user(u_today, today_start + timedelta(hours=1))

    u_this_week_not_today = make_user('reg_week')
    if week_start < today_start:
        backdate_user(u_this_week_not_today, week_start)
    else:
        # Monday — "this week, not today" is impossible; fold into today instead.
        backdate_user(u_this_week_not_today, today_start + timedelta(hours=2))

    u_last_month = make_user('reg_old')
    backdate_user(u_last_month, today_start - timedelta(days=40))

    counts = analytics._registration_counts(now)
    assert counts['today'] >= 1
    assert counts['this_week'] >= counts['today']
    assert counts['this_month'] >= counts['this_week']


def test_registration_growth_pct_compares_this_week_to_last_week():
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    prev_week_start = week_start - timedelta(days=7)

    for i in range(2):
        backdate_user(make_user(f'prevweek{i}'), prev_week_start + timedelta(days=1))
    for i in range(4):
        backdate_user(make_user(f'thisweek{i}'), week_start + timedelta(hours=1))

    counts = analytics._registration_counts(now)
    assert counts['this_week'] == 4
    # prev week had 2, this week has 4 -> +100%
    assert counts['growth_pct'] == 100.0


def test_registration_counts_exclude_fixed_support_account():
    """The support account (core/services/support.py, created by migration
    0045) is a system placeholder created the moment the test DB migrates —
    always "today" from the test's point of view. It must not count as a
    real signup, or every registration KPI is off by one forever."""
    now = timezone.now()
    assert User.objects.filter(username=SUPPORT_USERNAME).exists()

    counts = analytics._registration_counts(now)
    assert counts['today'] == 0
    assert counts['this_week'] == 0
    assert counts['this_month'] == 0

    series = analytics.registrations_timeseries(days=7)
    assert sum(day['value'] for day in series) == 0


# ---------------------------------------------------------------------------
# active users
# ---------------------------------------------------------------------------

def test_active_users_counts_only_recent_last_login():
    now = timezone.now()
    recent = make_user('active1')
    User.objects.filter(pk=recent.pk).update(last_login=now - timedelta(days=1))
    stale = make_user('inactive1')
    User.objects.filter(pk=stale.pk).update(last_login=now - timedelta(days=30))
    never = make_user('never_logged_in')

    assert analytics._active_users_count(now, days=7) == 1


# ---------------------------------------------------------------------------
# GMV
# ---------------------------------------------------------------------------

def test_gmv_counts_only_sold_statuses_within_window():
    owner, buyer = make_user('gmv_owner'), make_user('gmv_buyer')
    now = timezone.now()

    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'), status='confirmed', completed_at=now)
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('50'), status='completed', completed_at=now)
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('999'), status='disputed', completed_at=now)
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('999'), status='confirmed', completed_at=now - timedelta(days=40))

    stats = analytics._gmv_stats(now, days=30)
    assert stats['gmv'] == Decimal('150')
    assert stats['orders'] == 2


# ---------------------------------------------------------------------------
# disputes
# ---------------------------------------------------------------------------

def test_dispute_stats_open_count_and_avg_resolution_hours():
    owner, buyer = make_user('disp_owner'), make_user('disp_buyer')
    item = Item.objects.create(owner=owner, title='i', description='d', category=make_category(),
                                condition='good', quantity=1, status='active')
    now = timezone.now()

    Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='open1', description='d', status='open')
    Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='open2', description='d', status='investigating')

    resolved_fast = Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='r1', description='d', status='resolved')
    Dispute.objects.filter(pk=resolved_fast.pk).update(created_at=now - timedelta(hours=10), resolved_at=now - timedelta(hours=8))

    resolved_slow = Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title='r2', description='d', status='resolved')
    Dispute.objects.filter(pk=resolved_slow.pk).update(created_at=now - timedelta(hours=30), resolved_at=now - timedelta(hours=6))

    stats = analytics._dispute_stats(now, days=30)
    assert stats['open_count'] == 2
    # durations: 2h and 24h -> avg 13h
    assert stats['avg_resolution_hours'] == 13.0


def test_dispute_stats_avg_is_none_when_nothing_resolved_recently():
    stats = analytics._dispute_stats(timezone.now(), days=30)
    assert stats['open_count'] == 0
    assert stats['avg_resolution_hours'] is None


# ---------------------------------------------------------------------------
# withdrawals / live / commissions / stores
# ---------------------------------------------------------------------------

def test_withdrawal_stats_sums_only_pending():
    u = make_user('wd_user')
    WithdrawalRequest.objects.create(user=u, amount=Decimal('30'), status='pending')
    WithdrawalRequest.objects.create(user=make_user('wd_user2'), amount=Decimal('20'), status='pending')
    WithdrawalRequest.objects.create(user=make_user('wd_user3'), amount=Decimal('999'), status='approved')

    stats = analytics._withdrawal_stats()
    assert stats['pending_count'] == 2
    assert stats['pending_amount'] == Decimal('50')


def test_live_now_counts_only_live_sessions():
    store_owner = make_user('live_owner')
    store = UserStore.objects.create(user=store_owner, name='s')
    LiveSession.objects.create(store=store, title='l1', status='live', current_viewer_count=10)
    LiveSession.objects.create(store=store, title='l2', status='live', current_viewer_count=5)
    LiveSession.objects.create(store=store, title='l3', status='ended', current_viewer_count=999)

    stats = analytics._live_now_stats()
    assert stats['sessions'] == 2
    assert stats['viewers'] == 15


def test_commission_stats_sums_only_completed_in_window():
    rep = make_user('rep_user')
    now = timezone.now()
    Transaction.objects.create(user=rep, transaction_type='commission', amount=Decimal('12'), status='completed')
    Transaction.objects.create(user=rep, transaction_type='commission', amount=Decimal('999'), status='pending')
    Transaction.objects.create(user=rep, transaction_type='payout', amount=Decimal('999'), status='completed')
    old = Transaction.objects.create(user=rep, transaction_type='commission', amount=Decimal('999'), status='completed')
    Transaction.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=40))

    stats = analytics._representative_commission_stats(now, days=30)
    assert stats['total'] == Decimal('12')
    assert stats['count'] == 1


def test_active_stores_count():
    UserStore.objects.create(user=make_user('store_active'), name='a', is_active=True)
    UserStore.objects.create(user=make_user('store_inactive'), name='b', is_active=False)
    assert analytics._active_stores_count() == 1


def test_wallet_anomaly_count_catches_negative_and_overheld():
    from core.models import Wallet

    Wallet.objects.create(user=make_user('wallet_ok'), balance=Decimal('10'), held_balance=Decimal('5'))
    Wallet.objects.create(user=make_user('wallet_negative'), balance=Decimal('-1'))
    Wallet.objects.create(user=make_user('wallet_overheld'), balance=Decimal('10'), held_balance=Decimal('20'))

    assert analytics._wallet_anomaly_count() == 2


# ---------------------------------------------------------------------------
# timeseries
# ---------------------------------------------------------------------------

def test_registrations_timeseries_fills_every_day_including_zeros():
    series = analytics.registrations_timeseries(days=7)
    assert len(series) == 7
    assert series[-1]['date'] == timezone.now().date().isoformat()
    assert all('value' in point for point in series)


def test_sales_timeseries_places_totals_on_the_right_day():
    owner, buyer = make_user('ts_owner'), make_user('ts_buyer')
    today = timezone.now()
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('20'), status='confirmed', completed_at=today)

    series = analytics.sales_timeseries(days=7)
    assert series[-1]['value'] == 20.0
    assert sum(point['value'] for point in series[:-1]) == 0.0


# ---------------------------------------------------------------------------
# top categories / stores / governorates
# ---------------------------------------------------------------------------

def test_top_categories_orders_by_total_descending():
    owner, buyer = make_user('cat_owner'), make_user('cat_buyer')
    cat_a, _ = Category.objects.get_or_create(name='A', slug='a', defaults={'name_ar': 'أ'})
    cat_b, _ = Category.objects.get_or_create(name='B', slug='b', defaults={'name_ar': 'ب'})
    now = timezone.now()

    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('10'), status='confirmed', completed_at=now, category=cat_a)
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('90'), status='confirmed', completed_at=now, category=cat_b)

    rows = analytics.top_categories(limit=5, days=30)
    assert rows[0]['name'] == 'ب'
    assert rows[0]['total'] == Decimal('90')


def test_top_stores_orders_by_total_descending():
    owner_a, owner_b, buyer = make_user('store_a_owner'), make_user('store_b_owner'), make_user('store_buyer')
    UserStore.objects.create(user=owner_a, name='متجر أ')
    UserStore.objects.create(user=owner_b, name='متجر ب')
    now = timezone.now()

    make_reservation(owner=owner_a, buyer=buyer, total_price=Decimal('15'), status='confirmed', completed_at=now)
    make_reservation(owner=owner_b, buyer=buyer, total_price=Decimal('45'), status='confirmed', completed_at=now)

    rows = analytics.top_stores(limit=5, days=30)
    assert rows[0]['name'] == 'متجر ب'
    assert rows[0]['total'] == Decimal('45')


def test_active_governorates_includes_all_18_even_with_zero_orders():
    rows = analytics.active_governorates(days=30)
    assert len(rows) == len(IRAQ_GOVERNORATES) == 18
    assert all(row['orders'] == 0 for row in rows)


def test_active_governorates_ranks_by_order_count():
    owner, buyer = make_user('gov_owner'), make_user('gov_buyer')
    loc_baghdad = Location.objects.create(
        user=buyer, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )
    now = timezone.now()
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('30'), status='confirmed',
                      completed_at=now, delivery_location=loc_baghdad)
    make_reservation(owner=owner, buyer=buyer, total_price=Decimal('30'), status='confirmed',
                      completed_at=now, delivery_location=loc_baghdad)

    rows = analytics.active_governorates(days=30)
    assert rows[0]['name'] == 'بغداد'
    assert rows[0]['orders'] == 2
    assert rows[0]['total'] == Decimal('60')


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------

def test_compute_dashboard_data_includes_all_18_governorates_not_a_top_n():
    data = analytics._compute_dashboard_data()
    assert len(data['active_governorates']) == len(IRAQ_GOVERNORATES) == 18


def test_get_dashboard_data_is_cached(monkeypatch):
    calls = {'n': 0}
    original = analytics._compute_dashboard_data

    def counting_compute():
        calls['n'] += 1
        return original()

    monkeypatch.setattr(analytics, '_compute_dashboard_data', counting_compute)

    analytics.get_dashboard_data()
    analytics.get_dashboard_data()
    assert calls['n'] == 1


def test_get_dashboard_data_bypasses_cache_when_requested(monkeypatch):
    calls = {'n': 0}
    original = analytics._compute_dashboard_data

    def counting_compute():
        calls['n'] += 1
        return original()

    monkeypatch.setattr(analytics, '_compute_dashboard_data', counting_compute)

    analytics.get_dashboard_data(use_cache=False)
    analytics.get_dashboard_data(use_cache=False)
    assert calls['n'] == 2
