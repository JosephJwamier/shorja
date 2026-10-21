from django.db import transaction as db_transaction
from django.utils import timezone
from decimal import Decimal
from .models import Reservation, Transaction, get_or_create_wallet


def _lock_reservation(reservation):
    """Re-reads the row under a lock so the state check that follows is the
    one that counts. Every money move on a reservation goes through here:
    the buyer double-tapping 'complete', the warranty task racing the
    buyer's 'received', two 'cancel' taps — each would otherwise charge,
    pay or refund twice, because the view checked the status *before*
    opening its transaction."""
    return Reservation.objects.select_for_update().select_related('item').get(pk=reservation.pk)


def credit_earnings(user, amount, transaction_type, **transaction_kwargs):
    """
    Record a Transaction AND credit the user's wallet balance.
    Use for payout / commission (money the user RECEIVES).
    Call inside an outer transaction.atomic().
    """
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    txn = Transaction.objects.create(
        user=user,
        transaction_type=transaction_type,
        amount=amount,
        status='completed',
        **transaction_kwargs
    )
    wallet = get_or_create_wallet(user)
    wallet.credit(amount)
    return txn


def debit_balance(user, amount, transaction_type, **transaction_kwargs):
    """
    Debit the user's wallet AND record a Transaction.
    Use when a buyer SPENDS from balance (payment / purchase).
    Raises ValueError if insufficient funds. Call inside transaction.atomic().
    """
    amount = Decimal(str(amount))
    if amount <= 0:
        return None

    wallet = get_or_create_wallet(user)
    wallet.debit(amount)  # raises ValueError if not enough available balance

    return Transaction.objects.create(
        user=user,
        transaction_type=transaction_type,
        amount=amount,
        status='completed',
        **transaction_kwargs
    )


def refund_to_buyer(reservation, amount, item=None, reason=''):
    """Sends `amount` back where it actually came from, and returns what it did.

    The default is the buyer's wallet, because that is what paid. A financed
    order is the exception, and getting it wrong is the most expensive bug this
    feature can have: the buyer did not spend a dinar of their own on it — a
    financier did, and is deducting from their salary for ten months. Crediting
    their wallet would hand them the full price of the goods in cash while the
    deductions carried on.

    So a financed refund splits in two:

      to the buyer      only what the financier has already pulled from them
                        *and* handed the platform (InstallmentPlan.
                        collected_amount, net of what earlier refunds already
                        returned). That is the buyer's own money, sitting in
                        the platform's account.
      to the financier  the rest, as a reversal: stop collecting, take the
                        financing back. Requested here, sent after this
                        transaction commits — see installments.service.
                        request_reversal.

    Returns a dict of the split, for the caller's logs and tests. Call inside
    transaction.atomic(), like every other money mover here.
    """
    from .services.installments.service import live_plan_for, request_reversal

    amount = Decimal(str(amount))
    if amount <= 0:
        return {'to_buyer': Decimal('0.00'), 'to_financier': Decimal('0.00')}

    item = item if item is not None else reservation.item
    plan = live_plan_for(reservation)
    if plan is None:
        credit_earnings(
            user=reservation.buyer,
            amount=amount,
            transaction_type='refund',
            item=item,
            reservation=reservation,
        )
        return {'to_buyer': amount, 'to_financier': Decimal('0.00')}

    from .models import InstallmentPlan
    locked_plan = InstallmentPlan.objects.select_for_update().get(pk=plan.pk)
    owed_to_buyer = max(
        locked_plan.collected_amount - locked_plan.buyer_refunded_amount,
        Decimal('0.00'),
    )
    to_buyer = min(amount, owed_to_buyer)
    to_financier = amount - to_buyer

    if to_buyer > 0:
        credit_earnings(
            user=reservation.buyer,
            amount=to_buyer,
            transaction_type='refund',
            item=item,
            reservation=reservation,
        )
        locked_plan.buyer_refunded_amount += to_buyer
        locked_plan.save(update_fields=['buyer_refunded_amount', 'updated_at'])

    if to_financier > 0:
        request_reversal(locked_plan, to_financier,
                         reason=reason or 'استرداد حجز مموَّل #%d' % reservation.pk)

    return {'to_buyer': to_buyer, 'to_financier': to_financier}


def complete_reservation(reservation, payment_type):
    """
    Charge whatever the buyer still owes for their chosen payment_type and
    mark a pending reservation 'completed'. Shared by
    ReservationViewSet.complete (buyer actively chooses) and
    tasks.auto_complete_unclaimed_auction_reservations (auction winner who
    never chose in time — defaults to 'deposit', charging nothing further
    since the bid deposit already covers it).

    Must be called inside transaction.atomic(). Raises ValueError if the
    buyer can't cover what's still owed.
    """
    from .models import Item
    from .delivery_utils import compute_delivery_fee

    locked = _lock_reservation(reservation)
    if locked.status != 'pending':
        raise ValueError("Reservation is %s, not pending" % locked.status)
    # Continue on the caller's instance so its in-memory fields (a delivery
    # location attached moments ago) are what get saved.
    reservation.status = locked.status

    if reservation.auction_id:
        # Winning an auction already charged deposit_amount as the winning
        # bid's deposit while bidding (see auction_utils.create_winner_reservation).
        pay_now = (
            Decimal('0.00') if payment_type == 'deposit'
            else reservation.total_price - reservation.deposit_amount
        )
    else:
        pay_now = reservation.deposit_amount if payment_type == 'deposit' else reservation.total_price

    debit_balance(
        user=reservation.buyer,
        amount=pay_now,
        transaction_type='purchase',
        item=reservation.item,
        reservation=reservation
    )

    # Delivery fee follows the same timing the buyer chose for the item
    # itself: paid in-app now for a full payment, or deferred to cash-on-
    # delivery (bundled into the COD remainder — see
    # delivery_utils.create_outbound_delivery_order) alongside the rest for
    # a deposit-only one. 0 if there's no active delivery company yet.
    delivery_fee = compute_delivery_fee(reservation)
    if payment_type != 'deposit':
        debit_balance(
            user=reservation.buyer,
            amount=delivery_fee,
            transaction_type='delivery_fee',
            item=reservation.item,
            reservation=reservation
        )
    reservation.delivery_fee = delivery_fee
    # Snapshot the item's current warranty period so a later edit by the
    # seller doesn't retroactively change the terms of this order.
    reservation.warranty_days = reservation.item.warranty_days

    # 'completed' means the buyer finished their in-app payment step —
    # deposit-only (remainder collected in cash at delivery) or full —
    # regardless of which. 'confirmed' is a later, separate milestone: the
    # whole sale is settled (full amount collected + delivery confirmed by
    # the buyer, or by the warranty-period auto-release). See
    # approve_and_release_payment and tasks.release_payments_after_week.
    reservation.status = 'completed'
    reservation.is_deposit_only = (payment_type == 'deposit')
    reservation.completed_at = timezone.now()
    reservation.save(update_fields=['status', 'is_deposit_only', 'completed_at', 'delivery_fee', 'warranty_days'])

    # Stock is only taken now — not at reservation creation — so lock and
    # re-read it fresh (another reservation on the same item may have
    # completed in the meantime). Represented sales are the one exception:
    # their stock was already taken when the reservation was created.
    from .models import ItemVariant
    variant = reservation.stock_variant
    if variant is not None and not reservation.representative_id:
        variant = ItemVariant.objects.select_for_update().get(pk=variant.pk)
        # Clamp rather than raise: the reservation is already committed and
        # paid for by this point, and the old code clamped to zero here too.
        variant.take_stock(min(reservation.quantity, variant.quantity))
    item = Item.objects.select_for_update().get(id=reservation.item_id)
    item.sold_count += reservation.quantity
    item.save(update_fields=['sold_count'])

    # The sale is real from here on: write it into the seller's book so the
    # dashboard counts it alongside what they sell over the counter.
    from ledger.services import record_app_sale
    record_app_sale(reservation)


def release_reservation_payment(reservation, amount=None):
    """
    Release an already-collected reservation payment to the seller, splitting
    off the representative's commission if one is involved. Shared by the
    buyer-initiated release (approve_and_release_payment) and admin dispute
    resolution (pay_seller/no_action/partial_refund) so all pay out identically.

    `amount` overrides how much of the charge is paid out to the seller side
    (commission + net) — used by the partial_refund resolution, where only
    amount_charged minus the buyer's partial refund goes to the seller.
    Defaults to the full reservation.amount_charged.

    Caller is responsible for checking reservation.completed_at and
    reservation.payment_released_to_seller first, and for wrapping this in
    an outer transaction.atomic().
    """
    locked = _lock_reservation(reservation)
    if locked.payment_released_to_seller:
        # Already paid out by a concurrent caller — a second run here would
        # credit the seller (and representative, and courier) again.
        reservation.payment_released_to_seller = True
        reservation.payment_released_at = locked.payment_released_at
        return

    update_fields = ['payment_released_to_seller', 'payment_released_at']
    reservation.payment_released_to_seller = True
    reservation.payment_released_at = timezone.now()
    charged = amount if amount is not None else reservation.amount_charged

    # Settle the delivery fee off the top — earned once the sale reached
    # release, independent of the seller/representative split below. Always
    # comes off reservation.delivery_fee specifically (not the `amount`
    # override), so partial-refund math never touches it.
    #
    # Two ways the fee reaches the company, and they must not both run:
    #
    #   prepaid  the buyer paid the fee in-app, so the platform is holding
    #            it: credit the company its share here.
    #   COD      the company already took its share out of the cash it
    #            collected from the buyer and handed the platform the rest
    #            (DeliveryOrder.cash_due_to_platform). Crediting it again
    #            here would pay the company twice.
    #
    # Either way the platform's cut is credited from here: on COD it
    # physically arrived inside the cash settlement.
    if reservation.delivery_fee > 0:
        outbound = reservation.delivery_orders.filter(direction='outbound').first()
        company_account = outbound.company.account if outbound and outbound.company else None
        delivery_commission = outbound.platform_commission_amount if outbound else Decimal('0.00')
        company_share = reservation.delivery_fee - delivery_commission
        fee_already_taken_in_cash = bool(outbound and outbound.cash_collected
                                         and outbound.cash_to_collect > 0)

        if company_account and company_share > 0 and not fee_already_taken_in_cash:
            credit_earnings(
                user=company_account,
                amount=company_share,
                transaction_type='delivery_fee',
                item=reservation.item,
                reservation=reservation,
            )
        if delivery_commission > 0:
            from .services.commission import platform_account
            credit_earnings(
                user=platform_account(),
                amount=delivery_commission,
                transaction_type='commission',
                item=reservation.item,
                reservation=reservation,
            )
        # The whole gross fee leaves the sale pot either way: on COD the
        # company's share never entered it (amount_charged counts
        # cash_due_to_platform), on prepaid it was just paid out.
        charged -= (delivery_commission if fee_already_taken_in_cash
                    else reservation.delivery_fee)

    # How much of the whole charge is reaching the seller's side: 1 on a
    # normal sale, less on a partial refund. Computed once, before anything
    # is deducted, so every deduction below is measured against the same
    # base — otherwise the commission would be scaled to one number and the
    # installment fee to another.
    payout_scale = Decimal('1')
    if amount is not None:
        # Mirror of the subtraction above: on COD only the platform's cut of
        # the fee was ever part of amount_charged.
        outbound_for_base = reservation.delivery_orders.filter(direction='outbound').first()
        fee_in_pot = reservation.delivery_fee
        if outbound_for_base and outbound_for_base.cash_collected and outbound_for_base.cash_to_collect > 0:
            fee_in_pot = outbound_for_base.platform_commission_amount
        base_total = reservation.amount_charged - fee_in_pot
        payout_scale = (charged / base_total) if base_total > 0 else Decimal('1')

    # Platform commission comes off the seller's side, never the buyer's,
    # the courier's or the representative's. Frozen at creation
    # (services.commission.freeze_on_reservation); on a partial refund it is
    # scaled to what the seller side actually receives so the platform never
    # keeps a share of money that went back to the buyer.
    platform_cut = Decimal('0.00')
    if reservation.platform_commission_amount > 0 and charged > 0:
        platform_cut = (reservation.platform_commission_amount * payout_scale
                        ).quantize(Decimal('0.01'))
        platform_cut = min(platform_cut, charged)
        if platform_cut > 0:
            from .services.commission import platform_account
            credit_earnings(
                user=platform_account(),
                amount=platform_cut,
                transaction_type='commission',
                item=reservation.item,
                reservation=reservation,
            )
            charged -= platform_cut
            from ledger.services import record_platform_commission
            record_platform_commission(reservation, platform_cut)

    # رسم التقسيط: ثمن أن تُموَّل هذه البيعة، يحمله البائع كما يحمل عمولة
    # المنصة. والمنصة هي من دفعه فعلاً — المموّل خصمه من تسويته معها (انظر
    # InstallmentPlan.platform_settlement) — فيُقيَّد لحسابها لا لحساب
    # المموّل. صفر في أي بيعة غير مقسّطة، وصفر في النموذج الشهري حيث لا
    # يأخذ المموّل من التاجر شيئاً.
    from .services.installments.service import live_plan_for
    installment_plan = live_plan_for(reservation)
    if installment_plan is not None and installment_plan.seller_fee_amount > 0 and charged > 0:
        installment_fee = min(
            (installment_plan.seller_fee_amount * payout_scale).quantize(Decimal('0.01')),
            charged,
        )
        if installment_fee > 0:
            from .services.commission import platform_account
            credit_earnings(
                user=platform_account(),
                amount=installment_fee,
                transaction_type='installment_fee',
                item=reservation.item,
                reservation=reservation,
            )
            charged -= installment_fee
            from ledger.services import record_installment_fee
            record_installment_fee(reservation, installment_fee)

    if reservation.representative and reservation.commission_amount:
        seller_net = charged - reservation.commission_amount

        # Seller receives net amount (total minus representative commission)
        credit_earnings(
            user=reservation.item.owner,
            amount=seller_net,
            transaction_type='payout',
            item=reservation.item,
            reservation=reservation
        )

        reservation.commission_paid = True
        reservation.commission_paid_at = timezone.now()
        update_fields.extend(['commission_paid', 'commission_paid_at'])

        pending_commission = Transaction.objects.filter(
            user=reservation.representative,
            transaction_type='commission',
            reservation=reservation,
            status='pending'
        ).first()

        if pending_commission:
            # Mark transaction completed and credit wallet atomically
            pending_commission.status = 'completed'
            pending_commission.save(update_fields=['status'])
            rep_wallet = get_or_create_wallet(reservation.representative)
            rep_wallet.credit(reservation.commission_amount)
        else:
            credit_earnings(
                user=reservation.representative,
                amount=reservation.commission_amount,
                transaction_type='commission',
                item=reservation.item,
                reservation=reservation
            )
    else:
        # No representative — full charged amount goes to seller
        credit_earnings(
            user=reservation.item.owner,
            amount=charged,
            transaction_type='payout',
            item=reservation.item,
            reservation=reservation
        )

    reservation.save(update_fields=update_fields)
