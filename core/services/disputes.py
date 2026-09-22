"""
Dispute resolution — the only place a Dispute's resolution_action is
allowed to move money. Both the DRF endpoint (DisputeViewSet.resolve) and
the admin resolution console (core/admin.py DisputeAdmin) call
resolve_dispute() so there is exactly one implementation of what "refund
the buyer" or "pay the seller" actually does — extracted from what used to
be inline in the view (Phase 3) so the admin console can't drift from the
API's behavior.
"""
from django.db import transaction
from django.utils import timezone

from ..delivery_utils import create_return_delivery_order, get_active_delivery_company
from ..wallet_utils import credit_earnings, debit_balance, release_reservation_payment


class DisputeResolutionError(Exception):
    """A resolution_action that doesn't make sense for this dispute's
    current state (e.g. paying a seller who was never actually paid, or a
    partial refund larger than what the buyer was charged). Distinct from
    ValueError, which here means the money movement itself failed
    (insufficient balance) — see wallet_utils.debit_balance."""


def resolve_dispute(dispute, *, resolution_action, resolution='', admin_notes='',
                     partial_refund_amount=None):
    """
    Apply an admin's decision on a dispute: update its status/resolution
    fields and, for reservation-linked disputes, move money and settle the
    underlying reservation to match.

    Raises DisputeResolutionError for a resolution_action that doesn't fit
    the reservation's current state (nothing is written in that case).
    Raises ValueError if the money movement itself fails, e.g. the seller's
    balance can't cover a clawback — the transaction rolls back, so the
    dispute is left exactly as it was before this call.
    """
    reservation = dispute.reservation

    # Reject actions that don't make sense before touching anything
    if resolution_action == 'pay_seller' and reservation and not reservation.completed_at:
        raise DisputeResolutionError(
            'لا يمكن الدفع للبائع — الزبون لم يُتمّ دفع الحجز بعد'
        )

    if resolution_action == 'partial_refund' and reservation:
        if partial_refund_amount is None:
            raise DisputeResolutionError('partial_refund_amount is required for partial_refund')
        if partial_refund_amount > reservation.amount_charged:
            raise DisputeResolutionError('partial_refund_amount cannot exceed the amount charged')

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolution = resolution
        dispute.resolution_action = resolution_action
        dispute.admin_notes = admin_notes
        dispute.resolved_at = timezone.now()
        dispute.save()

        # Handle refund if approved
        if resolution_action == 'refund_buyer' and reservation:
            # amount_charged is 0 for a reservation that never reached
            # 'confirmed'/'completed' — nothing was ever taken from the
            # buyer, so there's nothing to move and no stock was held.
            refund_amount = reservation.amount_charged
            active_company = get_active_delivery_company()

            if refund_amount > 0 and active_company:
                # A return leg may already exist for this dispute — e.g. it
                # came from DeliveryOrderViewSet.refuse_receipt, which opens
                # a dispute AND schedules the return in the same call when
                # the buyer states a reason. Don't hand off a second one.
                return_order = reservation.delivery_orders.filter(
                    direction='return', dispute=dispute
                ).first()
                if return_order is None:
                    # The buyer may already hold the item physically (this is
                    # exactly the "damaged after receipt" case) — don't move
                    # any money yet. Hand a return leg to the delivery
                    # company; the refund fires from
                    # DeliveryOrderViewSet.pick_up once they confirm they
                    # took the item back from the buyer.
                    create_return_delivery_order(reservation, dispute=dispute)
                    # reservation stays 'disputed' until the return lands.
                elif return_order.status in ('picked_up', 'delivered'):
                    # The company already has the item back — this decision,
                    # just saved above, is the last piece needed. Execute
                    # the refund now instead of waiting on a pickup that
                    # already happened.
                    from ..delivery_utils import execute_return_refund
                    execute_return_refund(return_order)
                # else: still waiting on the company to confirm pickup — it
                # will refund then, now that resolution_action is saved as
                # 'refund_buyer' (see delivery_utils.execute_return_refund).
            else:
                if refund_amount > 0:
                    if reservation.payment_released_to_seller:
                        # Seller already holds the funds — claw them back
                        # first (raises ValueError if their balance can't
                        # cover it, which rolls back this whole resolution).
                        debit_balance(
                            user=reservation.item.owner,
                            amount=refund_amount,
                            transaction_type='refund',
                            item=reservation.item,
                            reservation=reservation
                        )
                    credit_earnings(
                        user=reservation.buyer,
                        amount=refund_amount,
                        transaction_type='refund',
                        item=reservation.item,
                        reservation=reservation
                    )

                    # Stock was taken when the reservation was
                    # confirmed/completed — restore it now.
                    from ..models import ItemVariant
                    stock_variant = reservation.stock_variant
                    if stock_variant is not None:
                        stock_variant = ItemVariant.objects.select_for_update().get(
                            pk=stock_variant.pk
                        )
                        stock_variant.return_stock(reservation.quantity)
                    from ledger.services import void_app_sale
                    void_app_sale(reservation, 'استرداد بعد نزاع')

                reservation.status = 'refunded'
                reservation.save(update_fields=['status'])

        elif resolution_action == 'pay_seller' and reservation:
            if not reservation.payment_released_to_seller:
                release_reservation_payment(reservation)
            # Dispute resolved in the seller's favor — the sale is settled.
            reservation.status = 'confirmed'
            reservation.save(update_fields=['status'])

        elif resolution_action == 'no_action' and reservation:
            # Dispute dismissed — the sale stands as-is and the
            # seller gets paid normally, same as if no dispute had
            # ever been filed.
            if not reservation.payment_released_to_seller:
                release_reservation_payment(reservation)
            reservation.status = 'confirmed'
            reservation.save(update_fields=['status'])

        elif resolution_action == 'partial_refund' and reservation:
            if reservation.payment_released_to_seller:
                # Seller already holds the funds — claw back the
                # partial amount first.
                debit_balance(
                    user=reservation.item.owner,
                    amount=partial_refund_amount,
                    transaction_type='refund',
                    item=reservation.item,
                    reservation=reservation
                )
                credit_earnings(
                    user=reservation.buyer,
                    amount=partial_refund_amount,
                    transaction_type='refund',
                    item=reservation.item,
                    reservation=reservation
                )
            else:
                credit_earnings(
                    user=reservation.buyer,
                    amount=partial_refund_amount,
                    transaction_type='refund',
                    item=reservation.item,
                    reservation=reservation
                )
                remainder = reservation.amount_charged - partial_refund_amount
                release_reservation_payment(reservation, amount=remainder)

            # Buyer keeps the item — this isn't a return, so stock
            # is not restored (unlike refund_buyer above).
            reservation.status = 'refunded'
            reservation.save(update_fields=['status'])

    return dispute
