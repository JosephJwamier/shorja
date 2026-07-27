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
from django.db import transaction
from django.utils import timezone

from ..models import Transaction, Wallet


class WithdrawalReviewError(Exception):
    """The withdrawal isn't 'pending' — already approved or rejected by an
    earlier action, so reviewing it again would double-settle the hold."""


def approve_withdrawal(withdrawal, *, reviewed_by, admin_notes=''):
    """Settle the held funds (removes from both held_balance and balance)
    and record the payout. Raises WithdrawalReviewError if not pending."""
    if withdrawal.status != 'pending':
        raise WithdrawalReviewError(f'Already {withdrawal.status}')

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
        wallet.settle_hold(withdrawal.amount)

        Transaction.objects.create(
            user=withdrawal.user,
            transaction_type='payout',  # money leaving balance to card
            amount=withdrawal.amount,
            status='completed',
        )
        withdrawal.status = 'approved'
        withdrawal.reviewed_by = reviewed_by
        withdrawal.reviewed_at = timezone.now()
        withdrawal.admin_notes = admin_notes
        withdrawal.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'admin_notes'])

    return withdrawal


def reject_withdrawal(withdrawal, *, reviewed_by, admin_notes=''):
    """Release the held funds back to spendable balance. Raises
    WithdrawalReviewError if not pending."""
    if withdrawal.status != 'pending':
        raise WithdrawalReviewError(f'Already {withdrawal.status}')

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(user=withdrawal.user)
        wallet.release_hold(withdrawal.amount)

        withdrawal.status = 'rejected'
        withdrawal.reviewed_by = reviewed_by
        withdrawal.reviewed_at = timezone.now()
        withdrawal.admin_notes = admin_notes
        withdrawal.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'admin_notes'])

    return withdrawal
