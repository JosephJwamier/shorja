"""Reads over the book: period summaries, the dashboard, and the two hooks
that mirror in-app sales into it."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from .models import Contact, LedgerEntry

ZERO = Decimal('0.00')
_DEC = DecimalField(max_digits=14, decimal_places=2)


def _sum(qs, **filters):
    return qs.filter(**filters).aggregate(
        t=Coalesce(Sum('amount'), Value(ZERO), output_field=_DEC))['t']


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def period_bounds(period, start=None, end=None):
    """Returns (start, end, previous_start, previous_end), all aware datetimes.
    `end` is exclusive. The previous window has the same length and ends
    where this one starts, for the "vs last period" comparison."""
    now = timezone.localtime()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        start, end = today, today + timedelta(days=1)
    elif period == 'week':
        start = today - timedelta(days=today.weekday() + 1 if today.weekday() != 6 else 0)  # Sunday
        end = start + timedelta(days=7)
    elif period == 'month':
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    elif period == 'year':
        start = today.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)
    elif period == 'custom' and start and end:
        end = end + timedelta(days=1)  # inclusive date → exclusive bound
    else:
        start, end = today.replace(day=1), (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    length = end - start
    return start, end, start - length, start


def summarize(store, start, end):
    """Everything the dashboard's money tiles need for one window."""
    live = LedgerEntry.objects.filter(store=store, is_void=False)
    window = live.filter(occurred_at__gte=start, occurred_at__lt=end)

    sales_cash = _sum(window, kind=LedgerEntry.KIND_SALE_CASH)
    sales_credit = _sum(window, kind=LedgerEntry.KIND_SALE_CREDIT)
    sales_app = _sum(window, kind=LedgerEntry.KIND_SALE_APP)
    payments_in = _sum(window, kind=LedgerEntry.KIND_PAYMENT_IN)
    expenses = _sum(window, kind=LedgerEntry.KIND_EXPENSE)
    purchases = _sum(window, kind=LedgerEntry.KIND_PURCHASE)
    payments_out = _sum(window, kind=LedgerEntry.KIND_PAYMENT_OUT)
    revenue = sales_cash + sales_credit + sales_app

    # Gross profit only where the cost is known; tell the caller how much
    # of the revenue that covers so a half-filled cost book isn't read as
    # "we made 100% margin on the rest".
    sales_with_cost = window.filter(kind__in=LedgerEntry.SALE_KINDS, cost_amount__isnull=False)
    costed_revenue = _sum(sales_with_cost)
    cost_of_sales = sales_with_cost.aggregate(
        t=Coalesce(Sum('cost_amount'), Value(ZERO), output_field=_DEC))['t']

    sales_count = window.filter(kind__in=LedgerEntry.SALE_KINDS).count()

    expenses_by_category = list(
        window.filter(kind=LedgerEntry.KIND_EXPENSE)
        .values('expense_category')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )

    # One point per day so the chart never has to bucket on the client.
    daily = (
        window.annotate(day=TruncDate('occurred_at'))
        .values('day')
        .annotate(
            local=Coalesce(Sum('amount', filter=Q(kind__in=(LedgerEntry.KIND_SALE_CASH, LedgerEntry.KIND_SALE_CREDIT))), Value(ZERO), output_field=_DEC),
            app=Coalesce(Sum('amount', filter=Q(kind=LedgerEntry.KIND_SALE_APP)), Value(ZERO), output_field=_DEC),
            out=Coalesce(Sum('amount', filter=Q(kind__in=(LedgerEntry.KIND_EXPENSE, LedgerEntry.KIND_PURCHASE))), Value(ZERO), output_field=_DEC),
        )
        .order_by('day')
    )

    return {
        'start': start, 'end': end,
        'revenue': revenue,
        'sales_cash': sales_cash,
        'sales_credit': sales_credit,
        'sales_app': sales_app,
        'sales_count': sales_count,
        'payments_in': payments_in,
        'expenses': expenses,
        'purchases': purchases,
        'payments_out': payments_out,
        # "What's left" in the plain sense a shopkeeper means it.
        'net': revenue - expenses - purchases,
        # Cash actually moved (a credit sale isn't cash yet; a payment is).
        'cash_in': sales_cash + sales_app + payments_in,
        'cash_out': expenses + payments_out,
        'cost_of_sales': cost_of_sales,
        'costed_revenue': costed_revenue,
        'gross_profit': (costed_revenue - cost_of_sales) if costed_revenue else None,
        'expenses_by_category': expenses_by_category,
        'daily': [
            {'day': d['day'], 'local': d['local'], 'app': d['app'], 'out': d['out']}
            for d in daily
        ],
    }


def open_balances(store):
    """All-time receivables / payables, independent of the period."""
    contacts = Contact.annotate_balance(
        Contact.objects.filter(store=store, is_archived=False)
    )
    receivables = ZERO
    payables = ZERO
    debtors = []
    creditors = []
    for c in contacts:
        if c.balance <= 0:
            continue
        if c.kind == Contact.KIND_CUSTOMER:
            receivables += c.balance
            debtors.append(c)
        else:
            payables += c.balance
            creditors.append(c)
    debtors.sort(key=lambda c: c.balance, reverse=True)
    creditors.sort(key=lambda c: c.balance, reverse=True)
    return {
        'receivables': receivables,
        'payables': payables,
        'debtors_count': len(debtors),
        'top_debtors': debtors[:5],
        'top_creditors': creditors[:5],
    }


def dashboard(store, period='month', start=None, end=None):
    """The store owner's one-screen view: money (from the book) plus the
    operational bits only the app knows (stock, orders, followers)."""
    from core.models import Item, ItemVariant, Reservation, StoreFollow

    s, e, ps, pe = period_bounds(period, start, end)
    current = summarize(store, s, e)
    previous = summarize(store, ps, pe)
    balances = open_balances(store)

    owner = store.user
    items = Item.objects.filter(owner=owner).exclude(status='archived')
    low_stock = (
        ItemVariant.objects.filter(item__in=items.filter(status__in=('active', 'sold')),
                                   is_active=True, quantity__lte=3)
        .select_related('item')
        .order_by('quantity', 'item__title')[:10]
    )
    # Orders the seller still has to act on: paid and waiting for handover.
    open_orders = Reservation.objects.filter(
        item__owner=owner, status='completed', payment_released_to_seller=False,
    ).count()
    pending_orders = Reservation.objects.filter(item__owner=owner, status='pending').count()

    top_items = (
        LedgerEntry.objects.filter(store=store, is_void=False, kind__in=LedgerEntry.SALE_KINDS,
                                   occurred_at__gte=s, occurred_at__lt=e, item__isnull=False)
        .values('item_id', 'item__title')
        .annotate(units=Coalesce(Sum('quantity'), Value(0)), total=Sum('amount'))
        .order_by('-total')[:5]
    )

    return {
        'period': {'key': period, 'start': s, 'end': e},
        'summary': current,
        'previous': {
            'revenue': previous['revenue'],
            'expenses': previous['expenses'],
            'net': previous['net'],
            'sales_count': previous['sales_count'],
        },
        'balances': balances,
        'stock': {
            'active_items': items.filter(status='active').count(),
            'sold_out_items': items.filter(status='sold').count(),
            'low_stock': list(low_stock),
        },
        'orders': {'pending': pending_orders, 'awaiting_handover': open_orders},
        'followers': StoreFollow.objects.filter(store=store).count(),
        'rating': store.rating,
        'top_items': list(top_items),
    }


# ---------------------------------------------------------------------------
# Mirroring in-app sales
# ---------------------------------------------------------------------------

def _store_for(reservation):
    return getattr(reservation.item.owner, 'store', None)


def record_app_sale(reservation):
    """Called once a reservation completes (buyer paid). Idempotent: the
    OneToOne on `reservation` means a second call is a no-op. Sellers
    without a store have no book to write in."""
    store = _store_for(reservation)
    if store is None or hasattr(reservation, 'ledger_entry'):
        return None
    variant = reservation.stock_variant
    cost = None
    if variant is not None and variant.cost_price is not None:
        cost = variant.cost_price * reservation.quantity
    note = reservation.item.title
    if reservation.representative_id:
        note += ' — عبر مندوب'
    return LedgerEntry.objects.create(
        store=store,
        kind=LedgerEntry.KIND_SALE_APP,
        amount=reservation.total_price,
        note=note[:255],
        occurred_at=reservation.completed_at or timezone.now(),
        item=reservation.item,
        variant=variant,
        quantity=reservation.quantity,
        cost_amount=cost,
        source=LedgerEntry.SOURCE_APP,
        reservation=reservation,
        created_by=None,
    )


def record_platform_commission(reservation, amount):
    """Books the platform's cut as an automatic expense in the seller's
    ledger the moment it is taken (payment release), so the dashboard's net
    matches what actually landed in the wallet. Idempotent per reservation."""
    store = _store_for(reservation)
    if store is None or amount <= 0:
        return None
    existing = LedgerEntry.objects.filter(
        store=store, kind=LedgerEntry.KIND_EXPENSE, expense_category='commission',
        source=LedgerEntry.SOURCE_APP, note__startswith='عمولة #%d ' % reservation.id,
    ).first()
    if existing:
        return existing
    return LedgerEntry.objects.create(
        store=store,
        kind=LedgerEntry.KIND_EXPENSE,
        expense_category='commission',
        amount=amount,
        note=('عمولة #%d — %s (%s%%)' % (reservation.id, reservation.item.title,
                                        reservation.platform_commission_percent))[:255],
        occurred_at=timezone.now(),
        item=reservation.item,
        source=LedgerEntry.SOURCE_APP,
        created_by=None,
    )


def record_installment_fee(reservation, amount):
    """Books the financier's merchant fee as an expense in the seller's
    ledger, the moment it is taken out of their payout.

    Filed under the 'commission' category rather than one of its own: to the
    seller it is the same kind of line — a platform fee deducted from an app
    sale — and the note says which fee it was. A new category would have
    bought a migration and a dashboard change to say the same thing.
    Idempotent per reservation.
    """
    store = _store_for(reservation)
    if store is None or amount <= 0:
        return None
    prefix = 'رسم تقسيط #%d ' % reservation.id
    existing = LedgerEntry.objects.filter(
        store=store, kind=LedgerEntry.KIND_EXPENSE, expense_category='commission',
        source=LedgerEntry.SOURCE_APP, note__startswith=prefix,
    ).first()
    if existing:
        return existing
    return LedgerEntry.objects.create(
        store=store,
        kind=LedgerEntry.KIND_EXPENSE,
        expense_category='commission',
        amount=amount,
        note=(prefix + '— %s (تقسيط كي كارد)' % reservation.item.title)[:255],
        occurred_at=timezone.now(),
        item=reservation.item,
        source=LedgerEntry.SOURCE_APP,
        created_by=None,
    )


def void_app_sale(reservation, reason):
    """The reservation was cancelled/refunded after completing: retire its
    ledger row so the book matches the money. Stock is handed back by the
    caller (the reservation flow already does that)."""
    entry = getattr(reservation, 'ledger_entry', None)
    if entry is None or entry.is_void:
        return
    with transaction.atomic():
        entry.void(reason)
