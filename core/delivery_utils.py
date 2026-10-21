"""
ReSell Trust - Delivery hand-off helpers

The platform does not manage the delivery company's drivers or internal
dispatch. A DeliveryOrder hands off the pickup/dropoff info needed and the
company reports back coarse status (picked up / delivered) through its one
linked account. These helpers build that hand-off and execute the
return-triggered refund used by refused deliveries and won disputes.
"""
from decimal import Decimal

from django.utils import timezone

from .models import (
    DeliveryBatch, DeliveryCompany, DeliveryOrder, DeliveryRate, Item, Location, Transaction,
)
from .notifications import (
    notify_delivery_assigned,
    notify_return_pickup_assigned,
    notify_return_refund_released,
)
from .wallet_utils import credit_earnings, debit_balance, refund_to_buyer


def get_active_delivery_company():
    return DeliveryCompany.objects.filter(is_active=True).first()


def _party_location(user):
    """Where this user ships from: their store's declared location if they
    set one, else their primary personal address."""
    store_location = getattr(getattr(user, 'store', None), 'default_location', None)
    if store_location is not None and store_location.is_active:
        return store_location
    return (
        Location.objects.filter(user=user, is_active=True)
        .order_by('-is_primary', '-created_at')
        .first()
    )


def resolve_pickup(item):
    """(location, custodian, contact) for collecting `item`.

    Ownership and custody are different questions, and delivery needs both.
    A wholesale transfer moves ownership with a click while the goods stay
    in the original seller's warehouse (see ItemTransfer: "The stock
    physically stays in the owner's warehouse"). Asking only "who owns
    this?" — as every caller here used to — sends the driver to the
    reseller's own address while the goods sit in another governorate, and
    prices the trip from the wrong city too.

    While a transfer is still running, the goods are at the original
    seller's store and that seller is the custodian (الخازن); the reseller
    remains the party the platform and the driver deal with. Once the term
    ends, custody follows ownership and the reseller's own address applies —
    which is also what happens for an item that was never transferred.

    `custodian` is None whenever the owner is holding their own goods.
    """
    contact = item.current_holder or item.owner
    transfer = (
        item.acquired_via_transfer
        .filter(status='active')
        .select_related('from_user')
        .order_by('-approved_at')
        .first()
    )
    if transfer is not None and not transfer.term_has_ended():
        custodian = transfer.from_user
        location = _party_location(custodian)
        if location is not None:
            return location, custodian, contact
        # A custodian with no address on file is worse than useless to a
        # driver — fall through to the owner rather than dispatch to nothing.
    return _party_location(contact), None, contact


def pickup_governorate(item):
    """The governorate the goods actually ship FROM — what the delivery
    tariff must be priced against."""
    location, _, _ = resolve_pickup(item)
    return location.state if location else ''


def compute_delivery_fee(reservation):
    """
    What the active delivery company charges for this reservation's
    delivery, based on whether the seller and the buyer's delivery address
    fall in the same governorate. Charged to the buyer in full at
    ReservationViewSet.complete (via complete_reservation), regardless of
    payment_type — see Reservation.delivery_fee. Returns 0 if there's no
    active company or no delivery address to compare against. Defaults to
    the (more conservative) inter-governorate rate if the seller has no
    location on file to compare — better to not under-charge the buyer
    relative to what the company will actually invoice for.
    """
    company = get_active_delivery_company()
    if not company or not reservation.delivery_location_id:
        return Decimal('0.00')

    from_gov = pickup_governorate(reservation.item)
    to_gov = reservation.delivery_location.state
    return quote_delivery_fee(company, from_gov, to_gov, reservation.item.package_size)


def quote_delivery_fee(company, from_governorate, to_governorate, package_size):
    """The company's price for one route and size.

    Most specific active DeliveryRate row wins; a blank field on a row is a
    wildcard. Fallback order is therefore: exact (from, to, size) → (from,
    to, *) → (*, to, size) … → (*, *, *) → the company's two flat fees,
    which keep working for a company that never filled the table. An
    unknown seller governorate matches only wildcard rows and, at the very
    end, the conservative inter-governorate flat fee.
    """
    candidates = [
        r for r in company.rates.filter(is_active=True)
        if (not r.from_governorate or r.from_governorate == from_governorate)
        and (not r.to_governorate or r.to_governorate == to_governorate)
        and (not r.package_size or r.package_size == package_size)
    ]
    if candidates:
        best = max(candidates, key=lambda r: (r.specificity, -float(r.fee)))
        return best.fee
    if from_governorate and from_governorate == to_governorate:
        return company.intra_governorate_fee
    return company.inter_governorate_fee


def create_outbound_delivery_order(reservation):
    """Hand a paid reservation's fulfilment off to the active delivery company."""
    item = reservation.item
    pickup, custodian, seller = resolve_pickup(item)
    company = get_active_delivery_company()

    dropoff = reservation.delivery_location

    cash_to_collect = Decimal('0.00')
    if reservation.is_deposit_only:
        # Deposit-only defers the delivery fee to COD too — see
        # wallet_utils.complete_reservation, which only charges it in-app
        # for a full payment.
        cash_to_collect = reservation.total_price - reservation.deposit_amount + reservation.delivery_fee

    # The platform's cut of this trip's fee, frozen now so a later
    # renegotiation with the company never re-prices a dispatched trip.
    from .services.commission import delivery_commission_for
    commission_percent, commission_amount = delivery_commission_for(
        company, reservation.delivery_fee)

    # On COD the company nets its own earnings straight out of the cash it
    # collects — it keeps (fee - commission) and hands the platform the
    # rest. cash_to_collect stays the gross the buyer pays; this is what
    # the platform's books actually chase.
    cash_due_to_platform = Decimal('0.00')
    if cash_to_collect > 0:
        cash_due_to_platform = cash_to_collect - reservation.delivery_fee + commission_amount

    order = DeliveryOrder.objects.create(
        reservation=reservation,
        direction='outbound',
        company=company,
        status='assigned',
        from_name=(pickup.contact_person if pickup else seller.username),
        from_phone=(pickup.contact_phone if pickup else seller.phone),
        from_address=(pickup.get_full_address() if pickup else ''),
        from_lat=(pickup.latitude if pickup else None),
        from_lng=(pickup.longitude if pickup else None),
        from_governorate=(pickup.state if pickup else ''),
        to_name=(dropoff.contact_person if dropoff else reservation.buyer.username),
        to_phone=(dropoff.contact_phone if dropoff else reservation.buyer.phone),
        to_address=(dropoff.get_full_address() if dropoff else ''),
        to_lat=(dropoff.latitude if dropoff else None),
        to_lng=(dropoff.longitude if dropoff else None),
        item_summary=item.title,
        quantity=reservation.quantity,
        # The driver is going to the custodian's warehouse but the platform's
        # counterparty is the reseller, so from_name/from_phone stay the
        # reseller's. Without this line the driver would arrive at a gate
        # whose keeper is not the person on the order.
        special_notes=(f'البضاعة مخزّنة لدى: {custodian.username}'
                       + (f' — {custodian.phone}' if custodian.phone else '')
                       if custodian else ''),
        cash_to_collect=cash_to_collect,
        cash_due_to_platform=cash_due_to_platform,
        delivery_fee=reservation.delivery_fee,
        platform_commission_percent=commission_percent,
        platform_commission_amount=commission_amount,
    )
    notify_delivery_assigned(order)
    return order


def pack_into_delivery_groups(reservations, max_units):
    """
    Greedily bins reservations into the fewest groups that each stay within
    max_units of combined Item.package_units (first-fit-decreasing) — so a
    seller's oversized batch still ships, just as more than one trip,
    instead of being rejected outright. Callers have already checked these
    reservations share the same seller/buyer/delivery address.
    """
    sized = sorted(reservations, key=lambda r: r.item.package_units, reverse=True)

    groups = []  # list of {'reservations': [...], 'units': int}
    for reservation in sized:
        units = reservation.item.package_units
        for group in groups:
            if group['units'] + units <= max_units:
                group['reservations'].append(reservation)
                group['units'] += units
                break
        else:
            groups.append({'reservations': [reservation], 'units': units})

    return [group['reservations'] for group in groups]


def combine_deliveries(reservations, company):
    """
    Bundle a group of same-seller/same-buyer/same-address reservations
    (already validated by the caller — completed, not yet sent to delivery,
    combined package_units within company.max_combined_units) into one
    DeliveryBatch. Still creates one DeliveryOrder per reservation exactly
    like create_outbound_delivery_order — per-item status/refusal/dispute
    tracking is untouched — but only the first reservation keeps its
    charged delivery_fee; the platform only actually owes the company one
    fee for the trip. The rest are waived: refunded from the buyer's wallet
    if the fee was already charged in-app, or simply left off their COD
    collection if deposit-only (both handled by zeroing
    Reservation.delivery_fee before create_outbound_delivery_order runs, so
    its existing cash_to_collect/delivery_fee computation just falls out
    correctly with no separate case needed here).

    Returns (batch, delivery_orders, total_refunded). Caller wraps in
    transaction.atomic().
    """
    fee_carrier = reservations[0]
    batch = DeliveryBatch.objects.create(
        buyer=fee_carrier.buyer,
        delivery_location=fee_carrier.delivery_location,
        company=company,
        combined_fee=fee_carrier.delivery_fee,
    )

    orders = []
    total_refunded = Decimal('0.00')
    for reservation in reservations:
        if reservation is not fee_carrier and reservation.delivery_fee > 0:
            waived_fee = reservation.delivery_fee
            if not reservation.is_deposit_only:
                refund_to_buyer(
                    reservation, waived_fee,
                    reason='أجرة توصيل مكرّرة — حجز #%d' % reservation.pk,
                )
                total_refunded += waived_fee
            reservation.delivery_fee = Decimal('0.00')
            reservation.save(update_fields=['delivery_fee'])

        order = create_outbound_delivery_order(reservation)
        order.batch = batch
        order.save(update_fields=['batch'])
        orders.append(order)

    return batch, orders, total_refunded


def create_return_delivery_order(reservation, dispute=None):
    """
    Spawn the reverse leg (buyer -> seller) for a refused delivery or a
    dispute resolved in the buyer's favour. The refund itself only fires
    once the company confirms it took the item back — see
    execute_return_refund, called from the return leg's pick-up action.
    """
    item = reservation.item
    # A return goes back to the warehouse the goods came out of — the
    # custodian's while a transfer is running, the owner's otherwise.
    dropoff, custodian, seller = resolve_pickup(item)
    company = get_active_delivery_company()

    pickup = reservation.delivery_location  # the buyer's address is now the pickup point

    order = DeliveryOrder.objects.create(
        reservation=reservation,
        direction='return',
        company=company,
        dispute=dispute,
        status='assigned',
        from_name=(pickup.contact_person if pickup else reservation.buyer.username),
        from_phone=(pickup.contact_phone if pickup else reservation.buyer.phone),
        from_address=(pickup.get_full_address() if pickup else ''),
        from_lat=(pickup.latitude if pickup else None),
        from_lng=(pickup.longitude if pickup else None),
        from_governorate=(pickup.state if pickup else ''),
        to_name=(dropoff.contact_person if dropoff else seller.username),
        to_phone=(dropoff.contact_phone if dropoff else seller.phone),
        to_address=(dropoff.get_full_address() if dropoff else ''),
        to_lat=(dropoff.latitude if dropoff else None),
        to_lng=(dropoff.longitude if dropoff else None),
        item_summary=item.title,
        quantity=reservation.quantity,
    )
    notify_return_pickup_assigned(order)
    return order


def execute_return_refund(delivery_order):
    """
    Called when a return leg reaches 'picked_up' — the company now has
    physical custody of the item back from the buyer. What happens to the
    money from here depends entirely on whether this return is tied to a
    Dispute (delivery_order.dispute_id):

    - Tied to a dispute (buyer stated a reason when refusing, or this is a
      dispute resolved 'refund_buyer' — see DisputeViewSet.resolve): an
      admin already decided the outcome, or will via that same resolve
      flow. Full refund, no automatic penalty second-guesses that call.

    - Not tied to a dispute (plain refusal at the door, no reason given):
      no complaint is on record, so the buyer forfeits their commitment —
      the deposit, plus the delivery fee too if they'd already paid it
      upfront (full payment, not deposit-only) — since the seller and the
      delivery company still did their part. Whatever's left above that is
      refunded. The forfeited deposit portion pays out to the seller and
      the forfeited fee portion to the delivery company, mirroring
      release_reservation_payment's split.

    Caller is responsible for wrapping this in transaction.atomic().
    """
    reservation = delivery_order.reservation
    dispute = delivery_order.dispute

    if dispute is not None and not (dispute.status == 'resolved' and dispute.resolution_action == 'refund_buyer'):
        # Physical custody is confirmed, but there's no refund decision to
        # act on yet: either the dispute is still open/investigating (an
        # admin hasn't ruled), or they ruled against a refund
        # (pay_seller/no_action/partial_refund already settled the money
        # separately via resolve_dispute). Nothing more to do here — see
        # resolve_dispute, which re-checks this same return order once
        # 'refund_buyer' is actually decided.
        return

    amount_charged = reservation.amount_charged
    refund_amount = amount_charged
    forfeited_deposit = Decimal('0.00')
    forfeited_fee = Decimal('0.00')

    if delivery_order.dispute_id is None:
        forfeited_deposit = min(reservation.deposit_amount, amount_charged)
        remaining = amount_charged - forfeited_deposit
        if not reservation.is_deposit_only:
            forfeited_fee = min(reservation.delivery_fee, remaining)
        refund_amount = amount_charged - forfeited_deposit - forfeited_fee

    forfeited_amount = forfeited_deposit + forfeited_fee

    if refund_amount > 0:
        if reservation.payment_released_to_seller:
            debit_balance(
                user=reservation.item.owner,
                amount=refund_amount,
                transaction_type='refund',
                item=reservation.item,
                reservation=reservation,
            )
        refund_to_buyer(
            reservation, refund_amount,
            reason='إرجاع المنتج — حجز #%d' % reservation.pk,
        )

    if forfeited_amount > 0:
        Transaction.objects.create(
            user=reservation.buyer,
            transaction_type='penalty',
            amount=forfeited_amount,
            status='completed',
            item=reservation.item,
            reservation=reservation,
            auction=reservation.auction,
        )
        if forfeited_deposit > 0:
            credit_earnings(
                user=reservation.item.owner,
                amount=forfeited_deposit,
                transaction_type='payout',
                item=reservation.item,
                reservation=reservation,
            )
        if forfeited_fee > 0 and delivery_order.company:
            # The trip was made, so the fee is earned and the platform's cut
            # applies exactly as on a successful delivery. This money comes
            # out of the buyer's forfeited deposit, never from cash the
            # company is holding, so both sides are credited here.
            from .services.commission import delivery_commission_for, platform_account
            _, forfeited_commission = delivery_commission_for(
                delivery_order.company, forfeited_fee)
            company_share = forfeited_fee - forfeited_commission
            if delivery_order.company.account and company_share > 0:
                credit_earnings(
                    user=delivery_order.company.account,
                    amount=company_share,
                    transaction_type='delivery_fee',
                    item=reservation.item,
                    reservation=reservation,
                )
            if forfeited_commission > 0:
                credit_earnings(
                    user=platform_account(),
                    amount=forfeited_commission,
                    transaction_type='commission',
                    item=reservation.item,
                    reservation=reservation,
                )
        reservation.payment_released_to_seller = True
        reservation.payment_released_at = timezone.now()
        reservation.save(update_fields=['payment_released_to_seller', 'payment_released_at'])

    if amount_charged > 0:
        from .models import ItemVariant
        stock_variant = reservation.stock_variant
        if stock_variant is not None:
            stock_variant = ItemVariant.objects.select_for_update().get(pk=stock_variant.pk)
            stock_variant.return_stock(reservation.quantity)
        from ledger.services import void_app_sale
        void_app_sale(reservation, 'أُعيد الطلب')
        item = Item.objects.select_for_update().get(id=reservation.item_id)
        # An auction's listing already ended when it was won — release the
        # item back to draft rather than 'active' (there's no live listing
        # selling it anymore, unlike a fixed-price/live-session item).
        item.status = 'draft' if reservation.auction_id else 'active'
        item.save(update_fields=['status'])

    reservation.status = 'refunded'
    reservation.save(update_fields=['status'])

    notify_return_refund_released(reservation, refund_amount)
