"""
Withdrawal review — the only place a WithdrawalRequest's held funds are
settled or released. Both the DRF endpoint (AdminWithdrawalViewSet) and the
admin review console (core/admin.py WithdrawalRequestAdmin) call these so
"approve" and "reject" mean exactly one thing everywhere, mirroring the
core/services/disputes.py pattern from Phase 3.

DepositRequest has no equivalent here: WalletViewSet.deposit is the only
path that ever touches it (a simulated payment gateway that completes
instantly), so there's no second implementation for it to drift from.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from ..models import (
    DeliveryOrder, Reservation, Transaction, User, Wallet, WithdrawalRequest, get_or_create_wallet,
)


# A number swapped in by whoever took the account cannot empty it at once.
PHONE_CHANGE_WAIT = timedelta(hours=48)
WHOLESALE_WAIT = timedelta(days=7)


def earnings_still_disputable(user):
    """What `user` earned from sales the buyer may still dispute. It can be
    spent, not withdrawn: a refund has to find the money somewhere."""
    now = timezone.now()
    # Reservation.dispute_deadline in SQL: a busy shop has thousands of sales,
    # and this runs whenever its wallet is opened. The warranty takes a handful
    # of values, so the deadline is one comparison for each.
    sales = Reservation.objects.filter(
        status__in=('confirmed', 'disputed'), payment_released_to_seller=True,
        payment_released_at__gte=now - timedelta(days=365 + settings.DISPUTE_GRACE_DAYS),
    ).filter(Q(item__owner=user) | Q(representative=user)).annotate(since=Coalesce(
        Subquery(DeliveryOrder.objects.filter(
            reservation=OuterRef('pk'), direction='outbound', delivered_at__isnull=False,
            status__in=('delivered', 'confirmed'),
        ).order_by('-delivered_at').values('delivered_at')[:1]),
        'buyer_approved_at', 'payment_released_at', 'completed_at'))
    still_open = Q(status='disputed')
    for days in set(sales.order_by().values_list('warranty_days', flat=True).distinct()):
        still_open |= Q(warranty_days=days, since__gte=now - timedelta(
            days=days + settings.DISPUTE_GRACE_DAYS))
    return Transaction.objects.filter(
        user=user, reservation__in=sales.filter(still_open).values('pk'), status='completed',
        transaction_type__in=('payout', 'commission'),
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')


def wholesale_still_settling(user):
    """Held wholesale money released in the last days. Released on a word from
    the other side, so without a wait two friends turn any balance into cash."""
    return Transaction.objects.filter(
        user=user, transaction_type='payout', status='completed', transfer__escrow=True,
        created_at__gte=timezone.now() - WHOLESALE_WAIT,
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')


def unspent_topups(user):
    """Card money that came in and was not spent. The wallet is for buying;
    paying a card's money out to another account is how stolen cards are
    emptied. Spending uses it first, and what is refunded becomes it again."""
    if user.topups_withdrawable:
        return Decimal('0.00')
    pot = spent = Decimal('0.00')
    mine = Transaction.objects.filter(user=user, status='completed')
    # Nothing before the first one can matter, and most sellers have none.
    first = mine.filter(
        Q(transaction_type__in=('payment', 'installment'))
        | Q(transaction_type='deposit', auction__isnull=True),
    ).order_by('created_at').values_list('created_at', flat=True).first()
    if first is None:
        return pot
    for t in (mine.filter(created_at__gte=first).exclude(transaction_type__in=('payout', 'commission'))
              .order_by('created_at', 'id').select_related('reservation', 'transfer')
              .only('transaction_type', 'amount', 'auction_id', 'reservation__buyer_id', 'transfer__to_user_id')):
        kind = t.transaction_type
        if kind in ('payment', 'installment') or (kind == 'deposit' and not t.auction_id):
            pot += t.amount
        elif kind in ('purchase', 'deposit', 'penalty') or (
                kind == 'storage_fee' and t.transfer_id and t.transfer.to_user_id == user.id) or (
                kind == 'delivery_fee' and t.reservation_id and t.reservation.buyer_id == user.id):
            # A penalty records money that already left as a purchase.
            used = min(pot, t.amount) if kind != 'penalty' else Decimal('0.00')
            pot -= used
            spent += used
        elif kind == 'refund' and (
                t.reservation.buyer_id == user.id if t.reservation_id
                else t.transfer.to_user_id == user.id if t.transfer_id else bool(t.auction_id)):
            back = min(spent, t.amount)
            pot += back
            spent -= back
    return pot


def not_withdrawable(user):
    """(amount, why) of what is in the wallet but may not leave it yet."""
    parts = [
        (earnings_still_disputable(user), 'أرباح لم تنتهِ مهلة النزاع عليها'),
        (wholesale_still_settling(user), 'ثمن جملة حُرِّر خلال آخر %d أيام' % WHOLESALE_WAIT.days),
        (unspent_topups(user), 'رصيد مشحون بالبطاقة للشراء لا للسحب'),
    ]
    parts = [(a, why) for a, why in parts if a > 0]
    return sum((a for a, _ in parts), Decimal('0.00')), '، و'.join(
        '%s (%s د.ع)' % (why, '{:,.0f}'.format(a)) for a, why in parts)


def withdrawable_balance(user):
    available = get_or_create_wallet(user).available_balance
    return max(available - not_withdrawable(user)[0], Decimal('0.00'))


def withdrawal_refusal(user):
    """Why this account may not withdraw at all right now, or None."""
    if not user.phone or not user.phone_verified:
        return 'وثّق رقم هاتفك أولاً من إعدادات الحساب'
    if user.phone_changed_at and timezone.now() < user.phone_changed_at + PHONE_CHANGE_WAIT:
        return 'غيّرت رقم هاتفك حديثاً — يُفتح السحب بعد 48 ساعة من التغيير'
    return None


class WithdrawalReviewError(Exception):
    """The withdrawal isn't 'pending' — already approved or rejected by an
    earlier action, so reviewing it again would double-settle the hold."""


def _lock_pending(withdrawal):
    """The status that counts is the row's under the lock, not the one the
    caller read earlier: two reviews of one request each saw 'pending'."""
    current = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal.pk)
    if current.status != 'pending':
        raise WithdrawalReviewError(f'Already {current.status}')


def approve_withdrawal(withdrawal, *, reviewed_by, admin_notes=''):
    """Settle the held funds (removes from both held_balance and balance)
    and record the payout. Raises WithdrawalReviewError if not pending."""
    with transaction.atomic():
        _lock_pending(withdrawal)
        if not User.objects.filter(pk=withdrawal.user_id, is_active=True).exists():
            raise WithdrawalReviewError('حساب صاحب الطلب موقوف — ارفض الطلب أو أعد تفعيل الحساب أولاً')
        wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
        wallet.settle_hold(withdrawal.amount)

        Transaction.objects.create(
            user=withdrawal.user,
            transaction_type='payout',  # money leaving balance to card
            amount=withdrawal.amount,
            status='completed',
        )
        withdrawal.status = 'approved'
        withdrawal.account_number = ''
        withdrawal.reviewed_by = reviewed_by
        withdrawal.reviewed_at = timezone.now()
        withdrawal.admin_notes = admin_notes
        withdrawal.save(update_fields=['status', 'account_number', 'reviewed_by', 'reviewed_at', 'admin_notes'])

    return withdrawal


def reject_withdrawal(withdrawal, *, reviewed_by, admin_notes=''):
    """Release the held funds back to spendable balance. Raises
    WithdrawalReviewError if not pending."""
    with transaction.atomic():
        _lock_pending(withdrawal)
        wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
        wallet.release_hold(withdrawal.amount)

        withdrawal.status = 'rejected'
        withdrawal.account_number = ''
        withdrawal.reviewed_by = reviewed_by
        withdrawal.reviewed_at = timezone.now()
        withdrawal.admin_notes = admin_notes
        withdrawal.save(update_fields=['status', 'account_number', 'reviewed_by', 'reviewed_at', 'admin_notes'])

    return withdrawal
