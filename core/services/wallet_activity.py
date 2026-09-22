"""The user's money, as a story: where each amount came from and where it went.

The wallet itself only knows two numbers (balance, held). What people ask is
"why is my available balance less than my balance?" and "where did the
15,000 go?" — so this module turns the rows that moved money (Transaction,
WithdrawalRequest, PaymentIntent) plus the money parked in escrow
(reservations paid but not yet released) into one timeline where every entry
has a direction and a from → to pair.

Directions:
    in       money that landed in the balance
    out      money that left it for good
    hold     money still counted in the balance but not spendable
             (a pending withdrawal), or paid and sitting in escrow
    pending  something that may become `in` (an unfinished card top-up)
    failed   a top-up that did not go through — shown so the user is not
             left wondering, never counted

Nothing here writes. Sync of pending gateway payments is the caller's job
(see payment_views.sync_pending_payments) and is deliberately manual.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from ..models import (
    PaymentIntent, Reservation, Transaction, WithdrawalRequest, get_or_create_wallet,
)

WALLET = "المحفظة"
CARD = "بطاقة (القاصة)"


def _res_label(reservation):
    title = reservation.item.title if reservation.item_id else "منتج"
    return "حجز #%d — %s" % (reservation.id, title)


def _money(value):
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _entry(*, id, kind, direction, amount, status, title, from_label, to_label,
           occurred_at, ref_type=None, ref_id=None, note=""):
    return {
        "id": id,
        "kind": kind,
        "direction": direction,
        "amount": _money(amount),
        "status": status,
        "title": title,
        "from": from_label,
        "to": to_label,
        "occurred_at": occurred_at,
        "ref_type": ref_type,
        "ref_id": ref_id,
        "note": note,
    }


def _transaction_entry(txn, user):
    """One Transaction row → one timeline entry, or None to skip it."""
    res = txn.reservation
    kind = txn.transaction_type
    when = txn.completed_at or txn.created_at
    base = dict(id="txn-%d" % txn.id, status=txn.status, occurred_at=when,
                ref_type="reservation" if res else ("auction" if txn.auction_id else None),
                ref_id=res.id if res else txn.auction_id)

    if kind == "payment":
        # Card top-up (gateway) or the legacy simulated one — money in.
        return _entry(kind="topup", direction="in", amount=txn.amount,
                      title="شحن المحفظة", from_label=CARD, to_label=WALLET, **base)

    if kind == "purchase" and res:
        # Paid for a reservation. Until the seller is paid it sits in escrow
        # and can still come back through a dispute — that is `hold`.
        if res.status == "refunded":
            direction, note = "out", "أُعيد المبلغ لاحقاً"
        elif res.payment_released_to_seller:
            direction, note = "out", "سُلّم للبائع"
        elif res.status in ("completed", "confirmed", "disputed"):
            direction, note = "hold", "في الضمان حتى استلامك وتأكيدك"
        else:
            direction, note = "out", ""
        return _entry(kind="purchase", direction=direction, amount=txn.amount,
                      title="دفع " + ("عربون" if res.is_deposit_only else "ثمن") + " حجز",
                      from_label=WALLET, to_label=_res_label(res), note=note, **base)

    if kind == "delivery_fee" and res:
        if res.buyer_id == user.id:
            return _entry(kind="delivery_fee", direction="out", amount=txn.amount,
                          title="أجرة توصيل", from_label=WALLET,
                          to_label="شركة التوصيل — " + _res_label(res), **base)
        return _entry(kind="delivery_fee", direction="in", amount=txn.amount,
                      title="أجرة توصيل", from_label=_res_label(res), to_label=WALLET, **base)

    if kind == "deposit":
        if txn.auction_id:
            return _entry(kind="bid_deposit", direction="out", amount=txn.amount,
                          title="عربون مزايدة", from_label=WALLET,
                          to_label="مزاد #%d" % txn.auction_id, **base)
        # Legacy gateway deposit rows (pending Stripe-era) — money that may come in.
        return _entry(kind="topup", direction="pending" if txn.status == "pending" else "in",
                      amount=txn.amount, title="إيداع", from_label=CARD, to_label=WALLET, **base)

    if kind == "payout":
        if res:
            return _entry(kind="sale", direction="in", amount=txn.amount,
                          title="أرباح بيع", from_label=_res_label(res), to_label=WALLET, **base)
        return _entry(kind="withdrawal", direction="out", amount=txn.amount,
                      title="سحب من المحفظة", from_label=WALLET, to_label="بطاقتك/حسابك", **base)

    if kind == "commission":
        direction = "in" if txn.status == "completed" else "hold"
        note = "" if txn.status == "completed" else "بانتظار تحرير المبلغ للبائع"
        return _entry(kind="commission", direction=direction, amount=txn.amount,
                      title="عمولة مندوب", from_label=_res_label(res) if res else "حجز",
                      to_label=WALLET, note=note, **base)

    if kind == "refund":
        return _entry(kind="refund", direction="in", amount=txn.amount,
                      title="استرداد", from_label=_res_label(res) if res else "النظام",
                      to_label=WALLET, **base)

    if kind == "penalty":
        return _entry(kind="penalty", direction="out", amount=txn.amount,
                      title="غرامة", from_label=WALLET,
                      to_label=_res_label(res) if res else "النظام", **base)

    if kind == "storage_fee":
        return _entry(kind="storage_fee", direction="out", amount=txn.amount,
                      title="رسوم تخزين", from_label=WALLET, to_label="المخزن", **base)

    if kind == "withdrawal":
        return _entry(kind="withdrawal", direction="out", amount=txn.amount,
                      title="سحب من المحفظة", from_label=WALLET, to_label="بطاقتك/حسابك", **base)

    return _entry(kind=kind, direction="out", amount=txn.amount, title=kind,
                  from_label=WALLET, to_label="", **base)


def _withdrawal_entries(user):
    """Pending withdrawals are the only thing that makes `held_balance` > 0;
    approved ones already appear as their payout Transaction."""
    out = []
    for w in WithdrawalRequest.objects.filter(user=user).exclude(status="approved"):
        if w.status == "pending":
            direction, title, note = "hold", "طلب سحب قيد المراجعة", "محجوز من رصيدك حتى البتّ"
        else:
            direction, title, note = "release", "طلب سحب مرفوض", "أُعيد المبلغ إلى رصيدك المتاح"
        out.append(_entry(
            id="wd-%d" % w.id, kind="withdrawal_request", direction=direction,
            amount=w.amount, status=w.status, title=title, from_label=WALLET,
            to_label="بطاقة ****%s" % w.card_last4 if w.card_last4 else "بطاقتك/حسابك",
            occurred_at=w.reviewed_at or w.created_at, ref_type="withdrawal", ref_id=w.id,
            note=note,
        ))
    return out


def pending_intents(user, within=timedelta(hours=48)):
    """Card top-ups the gateway has not settled — what the manual sync re-asks about."""
    since = timezone.now() - within
    return (PaymentIntent.objects
            .filter(user=user, created_at__gte=since)
            .exclude(state__in=(PaymentIntent.STATE_SUCCEEDED, PaymentIntent.STATE_FAILED,
                                PaymentIntent.STATE_CANCELLED))
            .order_by("-created_at"))


def _intent_entries(user):
    """Unsettled and failed top-ups from the last week. Succeeded ones are
    already in the timeline as their `payment` Transaction."""
    since = timezone.now() - timedelta(days=7)
    out = []
    for p in (PaymentIntent.objects.filter(user=user, created_at__gte=since, applied_at__isnull=True)
              .order_by("-created_at")):
        if p.state == PaymentIntent.STATE_SUCCEEDED:
            continue  # settled but not applied — can't happen outside a crash; skip
        failed = p.state in (PaymentIntent.STATE_FAILED, PaymentIntent.STATE_CANCELLED)
        out.append(_entry(
            id="pi-%d" % p.id, kind="topup", direction="failed" if failed else "pending",
            amount=p.amount, status=p.state,
            title="شحن المحفظة " + ("— لم يكتمل" if failed else "— بانتظار البوابة"),
            from_label=CARD, to_label=WALLET, occurred_at=p.created_at,
            ref_type="payment_intent", ref_id=p.reference,
            note="" if failed else "اضغط زر التحديث لإعادة السؤال عن حالتها",
        ))
    return out


def escrow_summary(user):
    """Money in flight on reservations: paid by this user and not yet handed to
    the seller (may still come back), and — as a seller/representative — earned
    but not yet released."""
    open_q = Q(completed_at__isnull=False, payment_released_to_seller=False,
               status__in=("completed", "confirmed", "disputed"))

    paid_waiting = Decimal("0")
    for r in Reservation.objects.filter(open_q, buyer=user).select_related("item"):
        paid_waiting += r.amount_charged

    earnings_waiting = Decimal("0")
    for r in (Reservation.objects.filter(open_q, item__owner=user)
              .select_related("item")):
        net = r.amount_charged - (r.delivery_fee or 0)
        if r.representative_id and r.commission_amount:
            net -= r.commission_amount
        earnings_waiting += max(net, Decimal("0"))

    commissions_waiting = Decimal("0")
    for r in Reservation.objects.filter(open_q, representative=user):
        commissions_waiting += r.commission_amount or 0

    return {
        "paid_waiting": _money(paid_waiting),
        "earnings_waiting": _money(earnings_waiting),
        "commissions_waiting": _money(commissions_waiting),
    }


def build_activity(user, limit=200):
    wallet = get_or_create_wallet(user)
    entries = []
    txns = (Transaction.objects.filter(user=user)
            .select_related("reservation", "reservation__item")
            .order_by("-created_at")[:limit])
    for txn in txns:
        e = _transaction_entry(txn, user)
        if e:
            entries.append(e)
    entries.extend(_withdrawal_entries(user))
    entries.extend(_intent_entries(user))
    entries.sort(key=lambda e: e["occurred_at"], reverse=True)

    return {
        "balance": _money(wallet.balance),
        "held_balance": _money(wallet.held_balance),
        "available_balance": _money(wallet.available_balance),
        "escrow": escrow_summary(user),
        "pending_topups": pending_intents(user).count(),
        "entries": entries[:limit],
    }
