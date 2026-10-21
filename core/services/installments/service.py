"""Quoting a plan, committing one, and unwinding one.

The whole feature funnels through this module, so there is exactly one place
that decides when financed money may move and exactly one rule about where it
goes back to.

**How a financed purchase reaches escrow.** It does not get its own payment
path. `confirm` credits the financed amount into the buyer's wallet and then
calls the ordinary `wallet_utils.complete_reservation`, which debits it again —
inside one transaction, so the net effect on the wallet is zero and there is no
instant in which the buyer could spend financed money on something else. Every
rule that already governs a paid reservation then applies unchanged: the money
sits in escrow, the seller is paid only after the buyer confirms receipt or the
warranty window passes, a dispute can still claw it back, and the platform's
commission still comes off the seller's side. A separate "installment payment"
path would have had to re-implement all of that, and would have drifted from it
within a release.

**Where a refund goes.** Not to the buyer's wallet — see
`wallet_utils.refund_to_buyer`. The financier paid for this order and is
deducting from the buyer's salary; crediting the wallet would hand the buyer
the price of the goods in cash while the deductions carried on. So the refund
splits: whatever the financier has already pulled from the buyer *and* handed
the platform is the buyer's own money and goes to their wallet, and the rest
goes back to the financier as a reversal.

**Why the provider is never called inside a money transaction.** `create_plan`
runs before the atomic block and `reverse_plan` runs after it commits
(`transaction.on_commit`). A bank call that hangs for thirty seconds inside the
block would hold a row lock on the reservation for thirty seconds, and every
other action on that order would queue behind it.
"""
import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    InstallmentCollection, InstallmentPlan, InstallmentReversal, Transaction,
    get_or_create_wallet,
)

from .base import InstallmentError, NotEligible
from .cards import last4, validate
from .registry import default_provider, get_provider

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal('0.01')
# How long a quote stands. Long enough to type a card number and read the
# schedule, short enough that nobody confirms yesterday's terms.
QUOTE_TTL_MINUTES = 15


class InstallmentNotAllowed(Exception):
    """This order cannot be financed — the amount, the channel or the state
    rules it out. Carries a code the app branches on and an Arabic message it
    can show as-is."""

    def __init__(self, code, message_ar):
        super().__init__(message_ar)
        self.code = code
        self.message_ar = message_ar


# ---------------------------------------------------------------------------
# What the app may show
# ---------------------------------------------------------------------------

def options():
    """The offer, before any card or order is involved.

    Returns None when no financier is configured, which is how the app knows
    to leave the installment button out entirely rather than show one that
    answers "unavailable".
    """
    provider = default_provider()
    if provider is None:
        return None
    terms = provider.terms
    return {
        'provider': provider.name,
        'label_ar': provider.label_ar,
        'months': terms.months,
        'customer_fee_percent': str(terms.customer_fee_percent),
        'min_amount': str(terms.min_amount),
        'max_amount': str(terms.max_amount),
        'settlement_mode': terms.settlement_mode,
    }


def price_preview(amount, provider=None):
    """The monthly figure for an amount, with no card and no order.

    This is the number that belongs next to a price in the app — "٢٥٠ ألف
    شهرياً" sells a 2.5-million television in a way "2,500,000" never will —
    so it has to be computable without starting a checkout.
    """
    provider = provider or default_provider()
    if provider is None:
        return None
    terms = provider.terms
    principal = _money(amount)
    if terms.min_amount and principal < terms.min_amount:
        return None
    if terms.max_amount and principal > terms.max_amount:
        return None
    fee = _percent_of(principal, terms.customer_fee_percent)
    total = principal + fee
    return {
        'months': terms.months,
        'monthly_amount': str(_monthly(total, terms.months)),
        'total_payable': str(total),
        'customer_fee_amount': str(fee),
    }


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------

def quote(*, user, reservation, card_number, provider_name=None):
    """Checks a card against this order and saves a `quoted` plan.

    The card number is validated locally, sent to the financier once, and then
    dropped: what the row keeps is the last four digits and the financier's
    token. Returns the saved [InstallmentPlan].

    Raises [InstallmentNotAllowed] when the order itself cannot be financed,
    [NotEligible] when the financier says no (both are answers for the buyer),
    [InstallmentError] for a fault, and [cards.InvalidCardNumber] for a typo.
    """
    provider = get_provider(provider_name) if provider_name else default_provider()
    if provider is None or not provider.is_configured:
        raise InstallmentNotAllowed('unavailable', 'التقسيط غير متاح حالياً')

    number = validate(card_number, lengths=provider.card_lengths,
                      require_luhn=provider.require_luhn)
    principal = financed_amount(reservation)
    _assert_financeable(reservation, principal, provider, user=user)

    eligibility = provider.check_eligibility(
        card_number=number, amount=principal, user=user)
    if eligibility.max_amount and principal > eligibility.max_amount:
        raise NotEligible('سقف التقسيط المتاح لبطاقتك %s د.ع فقط'
                          % _display(eligibility.max_amount), raw=eligibility.raw)

    terms = provider.terms
    months = eligibility.months or terms.months
    customer_percent = (eligibility.customer_fee_percent
                        if eligibility.customer_fee_percent is not None
                        else terms.customer_fee_percent)
    customer_fee = _percent_of(principal, customer_percent)
    total_payable = principal + customer_fee
    monthly = (_money(eligibility.monthly_amount) if eligibility.monthly_amount
               else _monthly(total_payable, months))

    merchant_fee = _percent_of(principal, terms.merchant_fee_percent)
    seller_fee = (merchant_fee * _seller_fee_share()).quantize(TWO_PLACES, ROUND_HALF_UP)

    # An older quote for the same reservation is no longer on offer. Expiring
    # it keeps the buyer's plan list honest and stops a stale reference being
    # confirmed after they re-checked with another card.
    (InstallmentPlan.objects
     .filter(reservation=reservation, state=InstallmentPlan.STATE_QUOTED)
     .update(state=InstallmentPlan.STATE_EXPIRED, updated_at=timezone.now()))

    return InstallmentPlan.objects.create(
        reservation=reservation,
        buyer=user,
        provider=provider.name,
        reference='INS-%s' % uuid.uuid4().hex[:20],
        state=InstallmentPlan.STATE_QUOTED,
        settlement_mode=terms.settlement_mode,
        principal=principal,
        months=months,
        customer_fee_percent=_money(customer_percent),
        customer_fee_amount=customer_fee,
        total_payable=total_payable,
        monthly_amount=monthly,
        merchant_fee_percent=terms.merchant_fee_percent,
        merchant_fee_amount=merchant_fee,
        seller_fee_amount=seller_fee,
        platform_settlement=principal - merchant_fee,
        card_last4=last4(number),
        card_holder_name=eligibility.card_holder_name,
        card_token=eligibility.card_token,
        last_provider_response=eligibility.raw,
        expires_at=timezone.now() + timedelta(minutes=QUOTE_TTL_MINUTES),
    )


def financed_amount(reservation):
    """What financing has to cover: the price plus the delivery fee.

    The fee is included because an installment purchase is a full payment —
    there is no cash-at-the-door half to defer it to — and because
    `complete_reservation` will charge it in the same breath as the price.
    Computed the same way that function computes it, so the credit and the
    debit cannot disagree.
    """
    from core.delivery_utils import compute_delivery_fee
    return (_money(reservation.total_price) + compute_delivery_fee(reservation)
            ).quantize(TWO_PLACES, ROUND_HALF_UP)


def _assert_financeable(reservation, principal, provider, user=None):
    """Everything about this order that can rule financing out, in one place."""
    if user is not None and reservation.buyer_id != user.id:
        raise InstallmentNotAllowed('not_buyer', 'هذا الحجز ليس لك')
    if reservation.status != 'pending':
        raise InstallmentNotAllowed(
            'not_pending', 'لا يمكن التقسيط — حالة الحجز %s' % reservation.status)
    if timezone.now() > reservation.expires_at:
        raise InstallmentNotAllowed('expired', 'انتهت مدة الحجز')
    if not reservation.delivery_location_id:
        raise InstallmentNotAllowed('no_address', 'أضف عنوان التسليم قبل التقسيط')
    if reservation.auction_id:
        # An auction winner already paid a bid deposit out of their wallet;
        # financing the rest would split one purchase across two funders and
        # make the refund arithmetic ambiguous for no gain.
        raise InstallmentNotAllowed('auction', 'التقسيط غير متاح لمزادات')

    terms = provider.terms
    if terms.min_amount and principal < terms.min_amount:
        raise InstallmentNotAllowed(
            'below_min', 'أقل مبلغ للتقسيط %s د.ع' % _display(terms.min_amount))
    if terms.max_amount and principal > terms.max_amount:
        raise InstallmentNotAllowed(
            'above_max', 'أعلى مبلغ للتقسيط %s د.ع' % _display(terms.max_amount))


# ---------------------------------------------------------------------------
# Confirming — the only place financed money is created
# ---------------------------------------------------------------------------

def confirm(*, user, reference):
    """Commits the quoted plan at the financier and completes the reservation.

    Returns the refreshed [InstallmentPlan]. Safe to call twice: a plan that
    already has `applied_at` comes straight back, so a double tap or a retried
    request cannot finance the same order twice.
    """
    plan = (InstallmentPlan.objects
            .select_related('reservation', 'reservation__item')
            .get(reference=reference, buyer=user))
    if plan.applied_at:
        return plan
    if plan.state != InstallmentPlan.STATE_QUOTED:
        raise InstallmentNotAllowed('stale_quote', 'انتهت صلاحية العرض — أعد الفحص')
    if timezone.now() > plan.expires_at:
        plan.state = InstallmentPlan.STATE_EXPIRED
        plan.save(update_fields=['state', 'updated_at'])
        raise InstallmentNotAllowed('stale_quote', 'انتهت صلاحية العرض — أعد الفحص')

    reservation = plan.reservation
    provider = get_provider(plan.provider)
    _assert_financeable(reservation, plan.principal, provider, user=user)

    # The quote was priced against a delivery fee that may have moved since
    # (a new rate, a changed address). Financing one number and charging
    # another would surface as "insufficient balance" on a purchase the buyer
    # is not paying for, so re-quote instead.
    if financed_amount(reservation) != plan.principal:
        plan.state = InstallmentPlan.STATE_EXPIRED
        plan.save(update_fields=['state', 'updated_at'])
        raise InstallmentNotAllowed('terms_changed',
                                    'تغيّر المبلغ الإجمالي — أعد فحص التقسيط')

    created = provider.create_plan(plan=plan)

    try:
        with transaction.atomic():
            locked = InstallmentPlan.objects.select_for_update().get(pk=plan.pk)
            if locked.applied_at:
                return locked
            _apply(locked, created, reservation)
    except Exception:
        # The plan exists at the financier but the order did not go through.
        # Leaving it would deduct ten months of salary for nothing.
        _reverse_orphan(plan, created)
        raise

    plan.refresh_from_db()
    return plan


def _apply(plan, created, reservation):
    """Money moves here, under the plan's row lock.

    Credit then debit, in one transaction: the financed amount lands in the
    buyer's wallet and `complete_reservation` immediately takes it into escrow.
    The wallet is a ledger, not a stop — see the module docstring.
    """
    from core.wallet_utils import complete_reservation

    plan.provider_plan_id = created.provider_plan_id
    plan.last_provider_response = created.raw
    if created.months:
        plan.months = created.months
    if created.monthly_amount:
        plan.monthly_amount = _money(created.monthly_amount)
    if created.total_payable:
        plan.total_payable = _money(created.total_payable)
        plan.customer_fee_amount = max(plan.total_payable - plan.principal,
                                       Decimal('0.00'))

    Transaction.objects.create(
        user=plan.buyer,
        transaction_type='installment',
        amount=plan.principal,
        status='completed',
        item=reservation.item,
        reservation=reservation,
        completed_at=timezone.now(),
    )
    get_or_create_wallet(plan.buyer).credit(plan.principal)

    # 'full' and never 'deposit': the financier settles the whole price, so
    # there is no cash-on-delivery remainder to collect. Raises ValueError if
    # the wallet cannot cover it, which can only mean the credit above and
    # this charge disagree — the re-quote check in confirm() exists to keep
    # that impossible, and rolling back is the right answer if it happens.
    complete_reservation(reservation, 'full')

    plan.state = InstallmentPlan.STATE_ACTIVE
    plan.applied_at = timezone.now()
    plan.save()
    logger.info('installment %s active: financed %s for reservation %s',
                plan.reference, plan.principal, reservation.pk)


def _reverse_orphan(plan, created):
    """Best-effort unwind of a plan created at the financier for an order that
    then failed. Recorded either way, so a failed unwind is visible in the
    admin rather than only in a log line."""
    plan.provider_plan_id = created.provider_plan_id
    plan.state = InstallmentPlan.STATE_FAILED
    plan.failure_reason = 'order did not complete; plan reversed'
    plan.save(update_fields=['provider_plan_id', 'state', 'failure_reason',
                             'updated_at'])
    reversal = InstallmentReversal.objects.create(
        plan=plan, amount=plan.principal,
        reason='الحجز لم يكتمل بعد إنشاء الخطة',
    )
    execute_reversal(reversal.pk)


# ---------------------------------------------------------------------------
# Collections — what the financier has actually taken
# ---------------------------------------------------------------------------

def sync_plan(plan):
    """Re-reads the plan from the financier and records what it reports.

    The authoritative read (see base.py). Collections are matched by
    `sequence`, so a replayed webhook, an out-of-order report or a full
    re-listing each converge on the same rows instead of double-counting.
    """
    provider = get_provider(plan.provider)
    status = provider.fetch_plan(plan=plan)

    with transaction.atomic():
        locked = InstallmentPlan.objects.select_for_update().get(pk=plan.pk)
        for row in status.collected:
            InstallmentCollection.objects.get_or_create(
                plan=locked, sequence=row.sequence,
                defaults={
                    'amount': _money(row.amount),
                    'collected_at': row.collected_at or timezone.now(),
                    'provider_reference': row.provider_reference,
                    'raw': row.raw,
                },
            )
        locked.collected_amount = sum(
            (c.amount for c in locked.collections.all()), Decimal('0.00'))
        locked.last_provider_response = status.raw
        # A reversed plan stays reversed: the financier may still report the
        # installments it took before the cancellation, and that is history,
        # not a reason to call the plan active again.
        if locked.state != InstallmentPlan.STATE_REVERSED:
            locked.state = status.state
        locked.save(update_fields=['collected_amount', 'state',
                                   'last_provider_response', 'updated_at'])
    plan.refresh_from_db()
    return plan


def plan_for_webhook(provider_name, payload, headers=None):
    """The plan a callback refers to, or None. Nothing else is read from the
    body — see base.py on why a callback is only ever a hint."""
    provider = get_provider(provider_name)
    reference = provider.parse_webhook(payload=payload, headers=headers or {})
    if not reference:
        return None
    return InstallmentPlan.objects.filter(reference=reference,
                                          provider=provider.name).first()


# ---------------------------------------------------------------------------
# Unwinding
# ---------------------------------------------------------------------------

def live_plan_for(reservation):
    """The financing behind this order, if any. `None` means the buyer paid
    from their own wallet and the ordinary refund path applies."""
    return (InstallmentPlan.objects
            .filter(reservation=reservation,
                    state__in=[InstallmentPlan.STATE_ACTIVE,
                               InstallmentPlan.STATE_SETTLED])
            .order_by('-applied_at').first())


def seller_fee_for(reservation):
    """What the seller owes for having this sale financed — deducted from
    their side at release, exactly like the platform's commission. Zero in the
    monthly settlement mode, where the financier charges the merchant nothing.
    """
    plan = live_plan_for(reservation)
    return plan.seller_fee_amount if plan else Decimal('0.00')


def request_reversal(plan, amount, reason=''):
    """Records that `amount` must go back to the financier, and sends it once
    the surrounding transaction commits.

    Deliberately split in two: the row is written inside the caller's
    transaction (so a rolled-back cancellation cannot leave a reversal
    behind), and the bank call happens after commit (so it cannot hold the
    reservation's row lock while a socket times out).
    """
    amount = _money(amount)
    if amount <= 0:
        return None
    locked = InstallmentPlan.objects.select_for_update().get(pk=plan.pk)
    amount = min(amount, locked.outstanding)
    if amount <= 0:
        return None
    reversal = InstallmentReversal.objects.create(
        plan=locked, amount=amount, reason=reason[:255])
    transaction.on_commit(lambda: execute_reversal(reversal.pk))
    return reversal


def execute_reversal(reversal_id):
    """Asks the financier to stop collecting and take the money back.

    Never raises: the order it belongs to has already been cancelled or
    refunded, and the platform's job here is to record the outcome. A refusal
    leaves the row `failed` for the admin queue — the money is then settled
    with the financier by hand, which is far better than crediting the buyer's
    wallet and discovering the reversal went through after all.
    """
    reversal = (InstallmentReversal.objects
                .select_related('plan').filter(pk=reversal_id).first())
    if reversal is None or reversal.state != InstallmentReversal.STATE_PENDING:
        return reversal

    plan = reversal.plan
    try:
        provider = get_provider(plan.provider)
        result = provider.reverse_plan(plan=plan, amount=reversal.amount,
                                       reason=reversal.reason)
    except Exception as exc:  # pragma: no cover - provider contract is no-raise
        logger.exception('installment reversal %s failed hard', reversal_id)
        result = None
        failure = str(exc)[:200]
    else:
        failure = result.failure_reason

    with transaction.atomic():
        row = InstallmentReversal.objects.select_for_update().get(pk=reversal_id)
        if row.state != InstallmentReversal.STATE_PENDING:
            return row
        locked_plan = InstallmentPlan.objects.select_for_update().get(pk=plan.pk)
        if result is not None and result.accepted:
            row.state = InstallmentReversal.STATE_CONFIRMED
            row.provider_reference = result.provider_reference
            row.raw = result.raw
            locked_plan.reversed_amount = (locked_plan.reversed_amount + row.amount)
            if locked_plan.reversed_amount >= locked_plan.principal:
                locked_plan.state = InstallmentPlan.STATE_REVERSED
            locked_plan.save(update_fields=['reversed_amount', 'state', 'updated_at'])
        else:
            row.state = InstallmentReversal.STATE_FAILED
            row.failure_reason = failure or 'المموّل لم يقبل الإرجاع'
            row.raw = result.raw if result is not None else None
            logger.error('installment reversal %s refused for plan %s: %s',
                         reversal_id, plan.reference, row.failure_reason)
        row.save(update_fields=['state', 'provider_reference', 'failure_reason',
                                'raw', 'updated_at'])
    return row


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _money(value):
    return Decimal(str(value or '0')).quantize(TWO_PLACES, ROUND_HALF_UP)


def _percent_of(amount, percent):
    return (_money(amount) * _money(percent) / Decimal('100')).quantize(
        TWO_PLACES, ROUND_HALF_UP)


def _monthly(total, months):
    """Rounded up to the whole dinar: fils do not exist in circulation, and a
    schedule whose installments sum to less than the total is a schedule that
    leaves a balance behind. The financier's own figure wins whenever it
    returns one."""
    months = max(int(months or 1), 1)
    return (_money(total) / Decimal(months)).quantize(Decimal('1'), ROUND_CEILING)


def _seller_fee_share():
    """How much of the financier's merchant fee the seller actually carries.

    1 (the default) passes it through in full; lower values are the platform
    buying installment adoption out of its own commission during a launch.
    """
    raw = getattr(settings, 'INSTALLMENT_SELLER_FEE_SHARE', '1')
    try:
        share = Decimal(str(raw))
    except Exception:
        return Decimal('1')
    return min(max(share, Decimal('0')), Decimal('1'))


def _display(amount):
    return '{:,}'.format(int(_money(amount)))
