"""
ReSell Trust - Auction settlement helpers

Winning an auction no longer settles money/ownership immediately. Instead it
turns into a Reservation the winner must complete — exactly like any other
sale — choosing between paying the remaining balance now or paying nothing
more (the bid deposit already covers the reservation's deposit_amount) and
the rest in cash at delivery. See ReservationViewSet.complete, which treats
an auction-linked reservation's deposit as already collected, and
wallet_utils.release_reservation_payment, which pays the seller once the
sale is actually settled — buyer approval, the warranty-period auto-release,
or the unclaimed-reservation timeout in
tasks.auto_complete_unclaimed_auction_reservations.
Item.owner is untouched throughout: an auction winner is just the
Reservation's buyer, same as any other sale.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import Bid, Location, Reservation

# How long a winner has to actively choose full-payment vs deposit+delivery
# before tasks.auto_complete_unclaimed_auction_reservations resolves it for
# them (deposit already paid stands, remainder collected in cash).
WINNER_RESPONSE_WINDOW_HOURS = 48


def refund_losing_deposits(auction, winning_bid):
    """Gives every losing *bidder* their one bid deposit back.

    The deposit is charged once per bidder on their first bid (see
    AuctionViewSet.place_bid) but every later bid row also carries
    deposit_paid=True. Refunding per row therefore paid a bidder once per
    bid — and paid the winner for each of their outbid rows too. So this
    works per bidder: one credit each, then every row of theirs is marked
    refunded so no later sweep can pay them again. The winner's rows are
    marked without a credit; their deposit stays committed to the sale.

    Must be called inside transaction.atomic(), with the auction row locked
    by the caller. Returns the number of bidders refunded.
    """
    from django.contrib.auth import get_user_model
    from .wallet_utils import credit_earnings

    deposit = auction.bid_deposit or Decimal('0')
    bidder_ids = set(
        auction.bids.filter(deposit_paid=True, deposit_refunded=False)
        .values_list('bidder_id', flat=True)
    )
    refunded = 0
    for bidder in get_user_model().objects.filter(id__in=bidder_ids):
        if bidder.id != winning_bid.bidder_id and deposit > 0:
            credit_earnings(user=bidder, amount=deposit, transaction_type='refund',
                            auction=auction)
            refunded += 1
        Bid.objects.filter(auction=auction, bidder=bidder).update(deposit_refunded=True)
    return refunded


def create_winner_reservation(auction, winning_bid):
    """
    Turn a settled auction into a pending Reservation for the winner.

    Must be called inside transaction.atomic(). Does not touch losing
    bidders' deposit refunds or auction.status/end_time (caller's
    responsibility, unchanged) and deliberately does not send the winner
    notification itself — callers send it (via notifications.notify_auction_won)
    only after their atomic block commits, same convention as the rest of
    this codebase (see place_bid, ReservationViewSet.complete).
    """
    winner = winning_bid.bidder

    delivery_location = (
        Location.objects.filter(user=winner, is_active=True)
        .order_by('-is_primary', '-created_at')
        .first()
    )

    from .services.commission import commission_for
    platform_percent, platform_amount = commission_for(auction.item, winning_bid.amount)
    reservation = Reservation.objects.create(
        item=auction.item,
        buyer=winner,
        quantity=1,
        deposit_amount=auction.bid_deposit or Decimal('0.00'),
        total_price=winning_bid.amount,
        expires_at=timezone.now() + timedelta(hours=WINNER_RESPONSE_WINDOW_HOURS),
        delivery_location=delivery_location,
        auction=auction,
        platform_commission_percent=platform_percent,
        platform_commission_amount=platform_amount,
    )

    auction.winner = winner
    auction.save(update_fields=['winner'])

    return reservation


def broadcast_live_auction_ended(auction, reservation):
    """
    Notify anyone watching the live session that this live-broadcast quick
    auction has settled — called by end_early, process_ended_auctions, and
    tasks.finalize_live_auction so all three report the result identically.
    No-op for a regular (non-live) auction.
    """
    if not auction.live_session_id:
        return

    from .consumers import broadcast_live_event
    broadcast_live_event(auction.live_session_id, 'auction_ended', {
        'auction_id': auction.id,
        'winner_username': auction.winner.username if auction.winner else None,
        'final_price': str(auction.current_price),
        'reservation_id': reservation.id if reservation else None,
    })
