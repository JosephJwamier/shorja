"""
Admin dashboard data (Phase 4). Every function here does one aggregation;
get_dashboard_data() composes them and is the only thing the dashboard
template/callback calls, cached for CACHE_TTL_SECONDS so opening the admin
index doesn't re-run a dozen aggregate queries on every request.

Note on "platform commission": the codebase has no platform take-rate —
release_reservation_payment() pays the seller the full charged amount
(minus a *representative's* commission when one is involved, which is
money that goes to that representative, not to bala). So this dashboard
reports GMV and representative commissions as separate, honest numbers
instead of a "platform revenue" figure that doesn't exist in the data.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from ..iraq_governorates import IRAQ_GOVERNORATES
from ..models import (
    Dispute, LiveSession, Reservation, Transaction, User, UserStore, Wallet,
    WithdrawalRequest,
)
from .support import SUPPORT_USERNAME

CACHE_KEY = 'admin_dashboard_data_v1'
CACHE_TTL_SECONDS = 5 * 60

# A reservation counts as an actual sale once it's been paid and not
# disputed away — 'confirmed' (settled) or 'completed' (paid, awaiting the
# buyer's receipt confirmation).
SOLD_STATUSES = ('confirmed', 'completed')


def _real_users():
    """Registered users, excluding the fixed system support account
    (core/services/support.py) — it's not a real signup and shouldn't
    inflate registration KPIs."""
    return User.objects.exclude(username=SUPPORT_USERNAME)


def _registration_counts(now):
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    prev_week_start = week_start - timedelta(days=7)

    today = _real_users().filter(created_at__gte=today_start).count()
    this_week = _real_users().filter(created_at__gte=week_start).count()
    this_month = _real_users().filter(created_at__gte=month_start).count()
    prev_week = _real_users().filter(
        created_at__gte=prev_week_start, created_at__lt=week_start
    ).count()

    if prev_week:
        growth_pct = round(((this_week - prev_week) / prev_week) * 100, 1)
    else:
        growth_pct = 100.0 if this_week else 0.0

    return {'today': today, 'this_week': this_week, 'this_month': this_month, 'growth_pct': growth_pct}


def _active_users_count(now, days=7):
    return User.objects.filter(last_login__gte=now - timedelta(days=days)).count()


def _gmv_stats(now, days=30):
    agg = Reservation.objects.filter(
        status__in=SOLD_STATUSES, completed_at__gte=now - timedelta(days=days),
    ).aggregate(total=Coalesce(Sum('total_price'), Decimal('0.00')), orders=Count('id'))
    return {'gmv': agg['total'], 'orders': agg['orders']}


def _dispute_stats(now, days=30):
    open_count = Dispute.objects.filter(status__in=('open', 'investigating')).count()
    pairs = Dispute.objects.filter(
        status='resolved', resolved_at__gte=now - timedelta(days=days)
    ).values_list('created_at', 'resolved_at')
    durations = [(resolved - created).total_seconds() for created, resolved in pairs if resolved]
    avg_hours = round((sum(durations) / len(durations)) / 3600, 1) if durations else None
    return {'open_count': open_count, 'avg_resolution_hours': avg_hours}


def _withdrawal_stats():
    agg = WithdrawalRequest.objects.filter(status='pending').aggregate(
        count=Count('id'), total=Coalesce(Sum('amount'), Decimal('0.00'))
    )
    return {'pending_count': agg['count'], 'pending_amount': agg['total']}


def _live_now_stats():
    agg = LiveSession.objects.filter(status='live').aggregate(
        sessions=Count('id'), viewers=Coalesce(Sum('current_viewer_count'), 0)
    )
    return {'sessions': agg['sessions'], 'viewers': agg['viewers']}


def _representative_commission_stats(now, days=30):
    agg = Transaction.objects.filter(
        transaction_type='commission', status='completed',
        created_at__gte=now - timedelta(days=days),
    ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')), count=Count('id'))
    return {'total': agg['total'], 'count': agg['count']}


def _active_stores_count():
    return UserStore.objects.filter(is_active=True).count()


def _wallet_anomaly_count():
    """Neither case should ever happen — Wallet.debit()/hold() check
    available_balance before moving anything — so any hit here means a bug
    or direct DB edit worth investigating, not normal operation."""
    return Wallet.objects.filter(Q(balance__lt=0) | Q(held_balance__gt=F('balance'))).count()


def _fill_daily_series(start_date, days, values_by_date, default=0.0):
    return [
        {
            'date': (start_date + timedelta(days=i)).isoformat(),
            'value': values_by_date.get(start_date + timedelta(days=i), default),
        }
        for i in range(days)
    ]


def registrations_timeseries(days=30):
    since_date = (timezone.now() - timedelta(days=days - 1)).date()
    rows = (
        _real_users().filter(created_at__date__gte=since_date)
        .annotate(day=TruncDate('created_at'))
        .values('day')
        .annotate(count=Count('id'))
    )
    counts = {row['day']: row['count'] for row in rows}
    return _fill_daily_series(since_date, days, counts, default=0)


def sales_timeseries(days=30):
    since_date = (timezone.now() - timedelta(days=days - 1)).date()
    rows = (
        Reservation.objects.filter(status__in=SOLD_STATUSES, completed_at__date__gte=since_date)
        .annotate(day=TruncDate('completed_at'))
        .values('day')
        .annotate(total=Coalesce(Sum('total_price'), Decimal('0.00')))
    )
    totals = {row['day']: float(row['total']) for row in rows}
    return _fill_daily_series(since_date, days, totals, default=0.0)


def top_categories(limit=5, days=30):
    since_date = (timezone.now() - timedelta(days=days)).date()
    rows = (
        Reservation.objects.filter(status__in=SOLD_STATUSES, completed_at__date__gte=since_date)
        .values('item__category__name_ar', 'item__category__name')
        .annotate(total=Coalesce(Sum('total_price'), Decimal('0.00')), orders=Count('id'))
        .order_by('-total')[:limit]
    )
    return [
        {
            'name': row['item__category__name_ar'] or row['item__category__name'] or '—',
            'total': row['total'],
            'orders': row['orders'],
        }
        for row in rows
    ]


def top_stores(limit=5, days=30):
    since_date = (timezone.now() - timedelta(days=days)).date()
    rows = (
        Reservation.objects.filter(status__in=SOLD_STATUSES, completed_at__date__gte=since_date)
        .values('item__owner__store__name')
        .annotate(total=Coalesce(Sum('total_price'), Decimal('0.00')), orders=Count('id'))
        .order_by('-total')[:limit]
    )
    return [
        {'name': row['item__owner__store__name'] or '—', 'total': row['total'], 'orders': row['orders']}
        for row in rows
    ]


def active_governorates(limit=None, days=30):
    """Every one of Iraq's 18 governorates, ranked by order count in the
    window — governorates with zero orders still appear (at zero), so this
    reads as coverage, not just a top-N leaderboard."""
    since_date = (timezone.now() - timedelta(days=days)).date()
    rows = (
        Reservation.objects.filter(
            status__in=SOLD_STATUSES, completed_at__date__gte=since_date,
            delivery_location__isnull=False,
        )
        .values('delivery_location__state')
        .annotate(total=Coalesce(Sum('total_price'), Decimal('0.00')), orders=Count('id'))
    )
    by_state = {row['delivery_location__state']: row for row in rows}

    ranked = [
        {
            'name': gov['name'],
            'total': by_state.get(gov['name'], {}).get('total', Decimal('0.00')),
            'orders': by_state.get(gov['name'], {}).get('orders', 0),
        }
        for gov in IRAQ_GOVERNORATES
    ]
    ranked.sort(key=lambda row: row['orders'], reverse=True)
    return ranked[:limit] if limit else ranked


def _compute_dashboard_data():
    now = timezone.now()
    return {
        'generated_at': now,
        'registrations': _registration_counts(now),
        'active_users': _active_users_count(now),
        'gmv': _gmv_stats(now),
        'disputes': _dispute_stats(now),
        'withdrawals': _withdrawal_stats(),
        'live_now': _live_now_stats(),
        'commissions': _representative_commission_stats(now),
        'active_stores': _active_stores_count(),
        'wallet_anomalies': _wallet_anomaly_count(),
        'registrations_series': registrations_timeseries(),
        'sales_series': sales_timeseries(),
        'top_categories': top_categories(),
        'top_stores': top_stores(),
        # All 18, not just a top-N — a governorate with zero orders is
        # itself useful information (where bala has no reach yet).
        'active_governorates': active_governorates(),
    }


def get_dashboard_data(use_cache=True):
    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached

    data = _compute_dashboard_data()
    cache.set(CACHE_KEY, data, CACHE_TTL_SECONDS)
    return data
