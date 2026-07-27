"""
ReSell Trust - Celery Tasks
Automated background jobs for reservations, auctions, notifications, and payment releases
"""

from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from decimal import Decimal
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)


@shared_task
def process_expired_reservations():
    """
    Mark reservations that were never completed (deposit/full paid) as
    expired. Stock isn't touched here — a 'pending' reservation never held
    any stock or money in the first place; that only happens once the buyer
    completes it (see ReservationViewSet.complete).

    Auction-won reservations are excluded — a winner who never actively
    chooses is defaulted into the deposit+cash-at-delivery option instead of
    expiring (see auto_complete_unclaimed_auction_reservations), since they
    already committed real money by winning the bid.
    """
    from .models import Reservation

    now = timezone.now()
    expired_reservations = Reservation.objects.filter(
        status='pending',
        expires_at__lt=now,
        auction__isnull=True
    )

    processed_count = 0
    for reservation in expired_reservations:
        try:
            with transaction.atomic():
                reservation.status = 'expired'
                reservation.save(update_fields=['status'])

                processed_count += 1
                logger.info(f"Processed expired reservation {reservation.id}")

        except Exception as e:
            logger.error(f"Error processing reservation {reservation.id}: {str(e)}")

    return f"Processed {processed_count} expired reservations"


@shared_task
def auto_complete_unclaimed_auction_reservations():
    """
    An auction winner has WINNER_RESPONSE_WINDOW_HOURS (see auction_utils) to
    actively choose full payment vs. deposit+cash-at-delivery via
    ReservationViewSet.complete. If they never do, default them into the
    deposit option automatically — they already paid the bid deposit and
    committed to the purchase by winning, so unlike a regular reservation
    this should not simply expire and release the item.
    """
    from .models import Reservation
    from .wallet_utils import complete_reservation
    from .notifications import notify_reservation_completed

    now = timezone.now()
    unclaimed = Reservation.objects.filter(
        status='pending',
        expires_at__lt=now,
        auction__isnull=False
    ).select_related('item', 'buyer', 'auction')

    processed_count = 0
    for reservation in unclaimed:
        try:
            with transaction.atomic():
                complete_reservation(reservation, 'deposit')
            processed_count += 1
            logger.info(
                f"Auto-completed unclaimed auction reservation {reservation.id} "
                f"(winner {reservation.buyer.username} did not choose in time — defaulted to deposit)"
            )
            try:
                notify_reservation_completed(reservation)
            except Exception as e:
                logger.error(f"Failed to send auto-complete notification for reservation {reservation.id}: {e}")
        except Exception as e:
            logger.error(f"Error auto-completing reservation {reservation.id}: {str(e)}")

    return f"Auto-completed {processed_count} unclaimed auction reservations"


@shared_task
def send_pending_reservation_reminders():
    """
    Nudge every buyer who still has a 'pending' (reserved but not yet paid)
    reservation — runs every 4 hours (see CELERY_BEAT_SCHEDULE) so a
    reservation doesn't just silently expire without a reminder in between.
    Reuses notify_reservation_expiring, which already saves an in-app
    Notification (type 'reservation_expiring') and sends the FCM push —
    tapping it takes the buyer straight to the reservations screen (see
    notification_navigator.dart).
    """
    from .models import Reservation
    from .notifications import notify_reservation_expiring

    now = timezone.now()
    pending = Reservation.objects.filter(
        status='pending',
        expires_at__gt=now,
    ).select_related('item', 'buyer')

    sent_count = 0
    for reservation in pending:
        try:
            hours_remaining = max(
                1, int((reservation.expires_at - now).total_seconds() / 3600)
            )
            notify_reservation_expiring(reservation, hours_remaining)
            sent_count += 1
        except Exception as e:
            logger.error(
                f"Error sending pending reminder for reservation {reservation.id}: {str(e)}"
            )

    return f"Sent pending-reservation reminders for {sent_count} reservations"


@shared_task
def notify_store_followers_live(session_id):
    """Fan out the 'store just went live' push to all its followers. Run via
    .delay() from LiveSessionViewSet.create/go_live so a store with many
    followers doesn't block the HTTP response."""
    from .models import LiveSession
    from .notifications import notify_live_stream_started

    try:
        session = LiveSession.objects.select_related('store').get(id=session_id)
        notify_live_stream_started(session)
        return f"Notified followers of store {session.store_id} about live session {session_id}"
    except LiveSession.DoesNotExist:
        logger.error(f"LiveSession {session_id} not found")
        return f"LiveSession {session_id} not found"


@shared_task
def auto_end_stale_live_sessions():
    """
    Safety net for a broadcast the owner never explicitly ended (app crash,
    lost connection, forgot). Ends any session still 'live' past
    LIVE_SESSION_MAX_DURATION_HOURS since it started.
    """
    from django.conf import settings
    from .models import LiveSession
    from .live_utils import end_live_session

    cutoff = timezone.now() - timedelta(
        hours=getattr(settings, 'LIVE_SESSION_MAX_DURATION_HOURS', 6)
    )
    stale_sessions = LiveSession.objects.filter(status='live', started_at__lt=cutoff)

    ended_count = 0
    for session in stale_sessions:
        try:
            with transaction.atomic():
                end_live_session(session, reason='auto_timeout')
            ended_count += 1
        except Exception as e:
            logger.error(f"Error auto-ending live session {session.id}: {str(e)}")

    return f"Auto-ended {ended_count} stale live sessions"


@shared_task
def release_payments_after_week():
    """
    Release payments to sellers after 1 week without disputes or buyer approval
    - Check completed reservations older than 7 days
    - Verify no open disputes
    - Release payment to seller (minus commission if applicable)
    - Release commission to representative if applicable
    """
    from .models import Reservation, Dispute
    from .wallet_utils import release_reservation_payment

    week_ago = timezone.now() - timedelta(days=7)

    reservations = Reservation.objects.filter(
        status__in=['completed', 'confirmed'],
        completed_at__lte=week_ago,
        payment_released_to_seller=False,
        buyer_approved=False
    ).select_related('item__owner', 'representative')

    released_count = 0
    for reservation in reservations:
        try:
            # Check if there are open disputes
            has_open_dispute = Dispute.objects.filter(
                reservation=reservation,
                status__in=['open', 'investigating']
            ).exists()

            if not has_open_dispute and reservation.can_release_payment():
                had_commission = bool(reservation.representative and reservation.commission_amount)

                with transaction.atomic():
                    release_reservation_payment(reservation)
                    # 7 days passed with no dispute — the sale is settled.
                    reservation.status = 'confirmed'
                    reservation.save(update_fields=['status'])

                logger.info(f"Auto-released payment for reservation {reservation.id}")
                if had_commission:
                    send_commission_paid_notification.delay(
                        reservation.id,
                        reservation.representative.id
                    )

                released_count += 1
                send_payment_release_notification.delay(reservation.id, 'auto_release')

        except Exception as e:
            logger.error(f"Error releasing payment for reservation {reservation.id}: {str(e)}")

    logger.info(f"Completed auto-release: Released payments for {released_count} reservations")
    return f"Released payments for {released_count} reservations"

@shared_task
def check_cancellation_eligibility():
    """
    Check reservations that are past 2-hour cancellation window
    Send notifications to buyers about cancellation restrictions
    """
    from .models import Reservation

    two_hours_ago = timezone.now() - timedelta(hours=2)
    two_hours_fifteen_ago = timezone.now() - timedelta(hours=2, minutes=15)

    # Get reservations that just passed the 2-hour mark (within last 15 minutes)
    recent_past_window = Reservation.objects.filter(
        status='pending',
        reserved_at__gte=two_hours_fifteen_ago,
        reserved_at__lte=two_hours_ago,
        seller_cancellation_approved=False
    )

    notified_count = 0
    for reservation in recent_past_window:
        try:
            send_cancellation_restriction_notification.delay(reservation.id)
            notified_count += 1
            logger.info(f"Sent cancellation restriction notice for reservation {reservation.id}")
        except Exception as e:
            logger.error(f"Error sending notification for reservation {reservation.id}: {str(e)}")

    return f"Sent {notified_count} cancellation restriction notifications"

@shared_task
def activate_upcoming_auctions():
    from django.utils import timezone
    from .models import AuctionListing

    now = timezone.now()

    updated = AuctionListing.objects.filter(
        status='upcoming',
        start_time__lte=now
    ).update(status='active')

    return f"Activated {updated} auctions"


@shared_task
def process_ended_auctions():
    """
    Process auctions that have ended automatically (time expired).
    - Determines the winner by is_winning flag (set during place_bid)
    - Turns the win into a pending Reservation the winner must complete
      (full payment now, or keep the bid deposit and pay the rest in cash
      at delivery) — see auction_utils.create_winner_reservation. Money and
      item ownership only move once that reservation actually settles.
    - Refunds bid_deposit to all losing bidders who paid it (unchanged,
      independent of what the winner ends up choosing)
    """
    from .models import AuctionListing
    from .auction_utils import create_winner_reservation, broadcast_live_auction_ended
    from .wallet_utils import credit_earnings

    now = timezone.now()
    ended_auctions = AuctionListing.objects.filter(
        status='active',
        end_time__lt=now
    ).select_related('item__owner')

    processed_count = 0
    for auction in ended_auctions:
        try:
            reservation = None
            with transaction.atomic():
                winning_bid = auction.bids.filter(is_winning=True).first()

                if winning_bid:
                    deposit = auction.bid_deposit or Decimal('0')

                    # Refund deposit to every losing bidder who paid it
                    losing_bids = auction.bids.filter(
                        deposit_paid=True,
                        deposit_refunded=False
                    ).exclude(id=winning_bid.id).select_related('bidder')

                    for bid in losing_bids:
                        credit_earnings(
                            user=bid.bidder,
                            amount=deposit,
                            transaction_type='refund',
                            auction=auction
                        )
                        bid.deposit_refunded = True
                        bid.save(update_fields=['deposit_refunded'])

                    reservation = create_winner_reservation(auction, winning_bid)

                    auction.status = 'ended'
                    auction.save(update_fields=['status'])

                    logger.info(
                        f"Auction {auction.id} won by {winning_bid.bidder.username} at "
                        f"{winning_bid.amount} — reservation {reservation.id} created, "
                        f"awaiting their payment choice."
                    )
                else:
                    # No bids — mark ended and release item
                    auction.status = 'ended'
                    auction.item.status = 'draft'
                    auction.item.save(update_fields=['status'])
                    auction.save(update_fields=['status'])

                processed_count += 1

            if reservation:
                from .notifications import notify_auction_won
                try:
                    notify_auction_won(auction, reservation)
                except Exception as e:
                    logger.error(f"Failed to send auction-won notification for auction {auction.id}: {e}")

            # Safety-net path only (the normal live-auction path settles via
            # finalize_live_auction's precise ETA task) — still report the
            # result if a live-linked auction somehow reaches this sweep.
            broadcast_live_auction_ended(auction, reservation)

        except Exception as e:
            logger.error(f"Error processing auction {auction.id}: {str(e)}")

    return f"Processed {processed_count} ended auctions"


@shared_task
def finalize_live_auction(auction_id):
    """
    Precisely settles a live-broadcast quick auction at its 2-minute mark
    (scheduled via .apply_async(eta=auction.end_time) from
    LiveSessionViewSet.quick_auction) rather than waiting for the next
    process_ended_auctions sweep, which runs once a minute — too slow for a
    live audience watching the countdown. That periodic sweep still acts as
    a safety net if this one-off task is ever missed (e.g. worker restart).

    Settlement itself is the exact same auction machinery as any other
    auction (AuctionViewSet._finalize_auction_payments): refund losing
    deposits, turn the win into a pending Reservation for the winner.
    """
    from .models import AuctionListing
    from .auction_utils import broadcast_live_auction_ended
    from .notifications import notify_auction_won

    try:
        auction = AuctionListing.objects.select_related('item__owner', 'live_session').get(id=auction_id)
    except AuctionListing.DoesNotExist:
        logger.error(f"finalize_live_auction: auction {auction_id} not found")
        return f"Auction {auction_id} not found"

    if auction.status != 'active' or timezone.now() < auction.end_time:
        return f"Auction {auction_id} already settled or not due yet — skipping"

    from .views import AuctionViewSet

    reservation = None
    try:
        with transaction.atomic():
            auction.status = 'ended'
            auction.save(update_fields=['status'])

            winning_bid = auction.bids.filter(is_winning=True).first()
            if winning_bid:
                reservation = AuctionViewSet._finalize_auction_payments(auction)
            else:
                auction.item.status = 'draft'
                auction.item.save(update_fields=['status'])

        if reservation:
            try:
                notify_auction_won(auction, reservation)
            except Exception as e:
                logger.error(f"Failed to send auction-won notification for live auction {auction.id}: {e}")

        broadcast_live_auction_ended(auction, reservation)

        winner_name = auction.winner.username if auction.winner else 'none'
        return f"Settled live auction {auction.id} (winner: {winner_name})"
    except Exception as e:
        logger.error(f"Error finalizing live auction {auction_id}: {str(e)}")
        return f"Error finalizing live auction {auction_id}: {str(e)}"


@shared_task
def auto_close_resolved_disputes():
    """
    Automatically close disputes that have been resolved for more than 7 days
    """
    from .models import Dispute

    week_ago = timezone.now() - timedelta(days=7)

    disputes_to_close = Dispute.objects.filter(
        status='resolved',
        resolved_at__lte=week_ago
    )

    closed_count = 0
    for dispute in disputes_to_close:
        try:
            dispute.status = 'closed'
            dispute.save(update_fields=['status'])
            closed_count += 1
            logger.info(f"Auto-closed dispute {dispute.id}")
        except Exception as e:
            logger.error(f"Error closing dispute {dispute.id}: {str(e)}")

    return f"Closed {closed_count} resolved disputes"


@shared_task
def calculate_user_trust_scores():
    """
    Recalculate trust scores for all users based on ratings
    """
    from .models import User

    users = User.objects.all()
    updated = 0

    for user in users:
        try:
            user.calculate_trust_score()
            updated += 1
        except Exception as e:
            logger.error(f"Error calculating trust score for user {user.id}: {str(e)}")

    return f"Updated trust scores for {updated} users"


@shared_task
def send_reservation_expiry_reminder(reservation_id):
    """
    Send reminder to buyer before reservation expires
    """
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        if reservation.status == 'pending':
            time_remaining = reservation.expires_at - timezone.now()
            hours_remaining = int(time_remaining.total_seconds() / 3600)

            message = (
                f"Reminder: Your reservation for '{reservation.item.title}' "
                f"expires in {hours_remaining} hours. "
                f"Please complete your purchase to avoid losing your reservation."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="Reservation Expiring Soon",
                message=message
            )

            logger.info(f"Sent expiry reminder for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending reminder for reservation {reservation_id}: {str(e)}")


@shared_task
def send_payment_release_notification(reservation_id, release_type='auto_release'):
    """
    Send notification when payment is released to seller
    Args:
        reservation_id: ID of the reservation
        release_type: 'auto_release', 'buyer_approved', or 'admin_approved'
    """
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        seller = reservation.item.owner

        if release_type == 'auto_release':
            subject = "Payment Released - 7 Days Passed"
            message = (
                f"Payment of ${reservation.total_price} for '{reservation.item.title}' "
                f"has been automatically released to you after 7 days without disputes."
            )
        elif release_type == 'buyer_approved':
            subject = "Payment Released - Buyer Approved"
            message = (
                f"Payment of ${reservation.total_price} for '{reservation.item.title}' "
                f"has been released after buyer approval."
            )
        else:  # admin_approved
            subject = "Payment Released - Admin Approved"
            message = (
                f"Payment of ${reservation.total_price} for '{reservation.item.title}' "
                f"has been released after admin dispute resolution."
            )

        NotificationService.send_email(
            to_email=seller.email,
            subject=subject,
            message=message
        )

        logger.info(f"Sent payment release notification for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending payment notification for reservation {reservation_id}: {str(e)}")


@shared_task
def send_cancellation_restriction_notification(reservation_id):
    """
    Notify buyer that 2-hour cancellation window has passed
    """
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        if reservation.status == 'pending' and not reservation.seller_cancellation_approved:
            message = (
                f"The 2-hour free cancellation window for '{reservation.item.title}' has passed. "
                f"You can no longer cancel this reservation without seller approval. "
                f"Please contact the seller if you need to cancel, or file a dispute if there's an issue."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="Cancellation Window Closed",
                message=message
            )

            logger.info(f"Sent cancellation restriction notice for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation notice for reservation {reservation_id}: {str(e)}")


@shared_task
def send_purchase_approval_reminder(reservation_id):
    """
    Remind buyer to approve purchase after completion
    Send at 1 day and 5 days after completion
    """
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        if reservation.status == 'completed' and not reservation.buyer_approved:
            days_since_completion = (timezone.now() - reservation.completed_at).days
            days_remaining = 7 - days_since_completion

            message = (
                f"Your purchase of '{reservation.item.title}' was completed {days_since_completion} day(s) ago. "
                f"If you're satisfied with your purchase, please approve it to release payment to the seller. "
                f"Payment will be automatically released in {days_remaining} days if no disputes are filed."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="Please Review Your Purchase",
                message=message
            )

            logger.info(f"Sent approval reminder for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending approval reminder for reservation {reservation_id}: {str(e)}")


@shared_task
def send_dispute_notification(dispute_id, notification_type='created'):
    """
    Send notifications related to disputes
    Args:
        dispute_id: ID of the dispute
        notification_type: 'created', 'resolved', 'investigating'
    """
    from .models import Dispute
    from .notifications import NotificationService

    try:
        dispute = Dispute.objects.get(id=dispute_id)

        if notification_type == 'created':
            # Notify defendant
            message = (
                f"A dispute has been filed against you regarding '{dispute.item.title}'. "
                f"Title: {dispute.title}. "
                f"Please check your dashboard for details."
            )
            NotificationService.send_email(
                to_email=dispute.defendant.email,
                subject="Dispute Filed",
                message=message
            )

        elif notification_type == 'resolved':
            # Notify both parties
            complainant_message = (
                f"Your dispute regarding '{dispute.item.title}' has been resolved. "
                f"Resolution: {dispute.resolution}"
            )
            defendant_message = (
                f"The dispute filed against you regarding '{dispute.item.title}' has been resolved. "
                f"Resolution: {dispute.resolution}"
            )

            NotificationService.send_email(
                to_email=dispute.complainant.email,
                subject="Dispute Resolved",
                message=complainant_message
            )
            NotificationService.send_email(
                to_email=dispute.defendant.email,
                subject="Dispute Resolved",
                message=defendant_message
            )

        elif notification_type == 'investigating':
            # Notify complainant
            message = (
                f"Your dispute regarding '{dispute.item.title}' is now under investigation. "
                f"We'll notify you once a decision has been made."
            )
            NotificationService.send_email(
                to_email=dispute.complainant.email,
                subject="Dispute Under Investigation",
                message=message
            )

        logger.info(f"Sent {notification_type} notification for dispute {dispute_id}")

    except Exception as e:
        logger.error(f"Error sending dispute notification {dispute_id}: {str(e)}")


@shared_task
def send_auction_ending_reminder(auction_id):
    """
    Send reminder to bidders that auction is ending soon
    """
    from .models import AuctionListing
    from .notifications import NotificationService

    try:
        auction = AuctionListing.objects.get(id=auction_id)

        if auction.status == 'active':
            time_remaining = auction.end_time - timezone.now()
            minutes_remaining = int(time_remaining.total_seconds() / 60)

            # Notify all bidders
            bidders = auction.bids.values_list('bidder__email', flat=True).distinct()

            message = (
                f"Auction for '{auction.item.title}' ends in {minutes_remaining} minutes! "
                f"Current price: ${auction.current_price}"
            )

            for email in bidders:
                NotificationService.send_email(
                    to_email=email,
                    subject="Auction Ending Soon",
                    message=message
                )

            logger.info(f"Sent ending reminder for auction {auction_id}")

    except Exception as e:
        logger.error(f"Error sending auction reminder {auction_id}: {str(e)}")


@shared_task
def cleanup_old_messages():
    """
    Clean up old system messages older than 90 days
    """
    from .models import Message

    cutoff_date = timezone.now() - timedelta(days=90)
    deleted_count = Message.objects.filter(
        message_type='system',
        created_at__lt=cutoff_date
    ).delete()[0]

    logger.info(f"Deleted {deleted_count} old system messages")
    return f"Deleted {deleted_count} messages"


@shared_task
def generate_sales_report(user_id, start_date, end_date):
    """
    Generate sales report for a user
    """
    from .models import User, Transaction

    try:
        user = User.objects.get(id=user_id)

        transactions = Transaction.objects.filter(
            user=user,
            transaction_type='payment',
            status='completed',
            created_at__range=[start_date, end_date]
        )

        total_sales = sum(t.amount for t in transactions)

        report_data = {
            'user': user.username,
            'period': f"{start_date} to {end_date}",
            'total_sales': float(total_sales),
            'transaction_count': transactions.count()
        }

        logger.info(f"Generated sales report for user {user_id}")
        return report_data

    except Exception as e:
        logger.error(f"Error generating sales report: {str(e)}")
        return None


@shared_task
def send_representative_request_notification(representative_id):
    """Notify owner about new representative request"""
    from .models import Representative
    from .notifications import NotificationService

    try:
        representative = Representative.objects.get(id=representative_id)

        message = (
            f"{representative.representative.username} wants to represent your item '{representative.item.title}'. "
            f"Commission: {representative.commission_percentage}%. "
            f"Message: {representative.request_message or 'No message provided'}"
        )

        NotificationService.send_email(
            to_email=representative.owner.email,
            subject="New Representative Request",
            message=message
        )

        logger.info(f"Sent representative request notification for {representative_id}")

    except Exception as e:
        logger.error(f"Error sending representative request notification: {str(e)}")


@shared_task
def send_representative_accepted_notification(representative_id):
    """Notify representative that request was accepted"""
    from .models import Representative
    from .notifications import NotificationService

    try:
        representative = Representative.objects.get(id=representative_id)

        message = (
            f"Your request to represent '{representative.item.title}' has been accepted! "
            f"Commission: {representative.commission_percentage}%. "
            f"You can now start representing this item."
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="Representative Request Accepted",
            message=message
        )

        logger.info(f"Sent acceptance notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending acceptance notification: {str(e)}")


@shared_task
def send_representative_rejected_notification(representative_id):
    """Notify representative that request was rejected"""
    from .models import Representative
    from .notifications import NotificationService

    try:
        representative = Representative.objects.get(id=representative_id)

        message = (
            f"Your request to represent '{representative.item.title}' has been rejected. "
            f"{f'Reason: {representative.rejection_reason}' if representative.rejection_reason else ''}"
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="Representative Request Rejected",
            message=message
        )

        logger.info(f"Sent rejection notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending rejection notification: {str(e)}")


@shared_task
def send_representative_cancelled_notification(representative_id, cancelled_by_user_id):
    """Notify party about cancellation"""
    from .models import Representative, User
    from .notifications import NotificationService

    try:
        representative = Representative.objects.get(id=representative_id)
        cancelled_by = User.objects.get(id=cancelled_by_user_id)

        # Determine who to notify
        if cancelled_by == representative.owner:
            notify_user = representative.representative
            message = f"The owner has cancelled your representative assignment for '{representative.item.title}'"
        else:
            notify_user = representative.owner
            message = f"{representative.representative.username} has cancelled their representative request for '{representative.item.title}'"

        NotificationService.send_email(
            to_email=notify_user.email,
            subject="Representative Assignment Cancelled",
            message=message
        )

        logger.info(f"Sent cancellation notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation notification: {str(e)}")


@shared_task
def send_representative_permissions_updated_notification(representative_id):
    """Notify representative about permission changes"""
    from .models import Representative
    from .notifications import NotificationService

    try:
        representative = Representative.objects.get(id=representative_id)

        message = (
            f"Your permissions for representing '{representative.item.title}' have been updated. "
            f"Can list: {representative.can_list}, "
            f"Can negotiate: {representative.can_negotiate}, "
            f"Can complete sale: {representative.can_complete_sale}, "
            f"Commission: {representative.commission_percentage}%"
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="Representative Permissions Updated",
            message=message
        )

        logger.info(f"Sent permissions update notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending permissions notification: {str(e)}")


@shared_task
def send_payment_release_request_notification(reservation_id, seller_username, buyer_id):
    """Send notification to buyer when seller requests payment release"""
    from .models import Reservation, User
    from .notifications import NotificationService, notify_push

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        buyer = User.objects.get(id=buyer_id)

        message = (
            f"{seller_username} has requested that you approve and release payment for your completed reservation. "
            f"Item: {reservation.item.title}, "
            f"Amount: ${reservation.total_price}. "
            f"If you're satisfied with the transaction, please approve the reservation to release payment to the seller."
        )

        NotificationService.send_email(
            to_email=buyer.email,
            subject=f"Payment Release Request - Reservation #{reservation_id}",
            message=message
        )

        notify_push(
            buyer,
            title='طلب تحرير الدفعة',
            body=f'{seller_username} يطلب تأكيدك على استلام "{reservation.item.title}" لتحرير المبلغ له.',
            data={
                'type': 'payment_release_requested',
                'reservation_id': str(reservation.id),
                'item_title': reservation.item.title,
            },
        )

        logger.info(f"Payment release request notification sent to buyer {buyer_id} for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending payment release request notification: {str(e)}")


@shared_task
def send_payment_released_notification(reservation_id, seller_id):
    """Send notification to seller when payment is released"""
    from .models import Reservation, User
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        seller = User.objects.get(id=seller_id)

        message = (
            f"Payment has been released for reservation #{reservation_id}. "
            f"Item: {reservation.item.title}, "
            f"Amount: ${reservation.total_price}, "
            f"Buyer: {reservation.buyer.username}. "
            f"The payment is now available in your account."
        )

        NotificationService.send_email(
            to_email=seller.email,
            subject=f"Payment Released - Reservation #{reservation_id}",
            message=message
        )

        logger.info(f"Payment released notification sent to seller {seller_id} for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending payment released notification: {str(e)}")


@shared_task
def send_cancellation_restriction_notification(reservation_id):
    """Send notification to buyer about cancellation restrictions"""
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        message = (
            f"The 2-hour cancellation window for your reservation has expired. "
            f"Item: {reservation.item.title}, "
            f"Reservation ID: #{reservation_id}. "
            f"You can no longer cancel this reservation without seller approval. Please complete the purchase or contact the seller."
        )

        NotificationService.send_email(
            to_email=reservation.buyer.email,
            subject=f"Cancellation Period Expired - Reservation #{reservation_id}",
            message=message
        )

        logger.info(f"Sent cancellation restriction notice for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation restriction notification: {str(e)}")


@shared_task
def send_reservation_expired_notification(reservation_id):
    """Send notification when reservation expires"""
    from .models import Reservation
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        # Notify buyer
        buyer_message = (
            f"Your reservation has expired. "
            f"Item: {reservation.item.title}, "
            f"Reservation ID: #{reservation_id}. "
            f"The reserved quantity has been returned to available stock."
        )

        NotificationService.send_email(
            to_email=reservation.buyer.email,
            subject=f"Reservation Expired - Reservation #{reservation_id}",
            message=buyer_message
        )

        # Notify seller
        seller_message = (
            f"A reservation for your item has expired. "
            f"Item: {reservation.item.title}, "
            f"Reservation ID: #{reservation_id}, "
            f"Buyer: {reservation.buyer.username}. "
            f"The item is now available for other buyers."
        )

        NotificationService.send_email(
            to_email=reservation.item.owner.email,
            subject=f"Reservation Expired - {reservation.item.title}",
            message=seller_message
        )

        logger.info(f"Sent expiration notifications for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending reservation expired notification: {str(e)}")


@shared_task
def send_reservation_cancelled_notification(reservation_id, cancelled_by_user_id):
    """Notify parties about reservation cancellation"""
    from .models import Reservation, User
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        cancelled_by = User.objects.get(id=cancelled_by_user_id)

        # Determine who to notify
        if cancelled_by == reservation.buyer:
            notify_user = reservation.item.owner
            message = (
                f"{reservation.buyer.username} has cancelled their reservation for your item. "
                f"Item: {reservation.item.title}, "
                f"Reservation ID: #{reservation_id}, "
                f"Quantity: {reservation.quantity}. "
                f"The item is now available for other buyers."
            )
        else:
            notify_user = reservation.buyer
            message = (
                f"Your reservation has been cancelled by the seller. "
                f"Item: {reservation.item.title}, "
                f"Reservation ID: #{reservation_id}. "
                f"Any deposit paid will be refunded."
            )

        NotificationService.send_email(
            to_email=notify_user.email,
            subject=f"Reservation Cancelled - Reservation #{reservation_id}",
            message=message
        )

        logger.info(f"Sent cancellation notification for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation notification: {str(e)}")


@shared_task
def send_representative_sale_notification(reservation_id, representative_assignment_id):
    """
    Notify representative that they made a sale and will earn commission
    """
    from .models import Reservation, Representative
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer', 'representative'
        ).get(id=reservation_id)

        representative_assignment = Representative.objects.select_related(
            'representative', 'item'
        ).get(id=representative_assignment_id)

        representative = representative_assignment.representative

        message = (
            f"Great news! {reservation.buyer.username} purchased '{reservation.item.title}' through your representative link. "
            f"Sale amount: ${reservation.total_price}, "
            f"Your commission: ${reservation.commission_amount} ({representative_assignment.commission_percentage}%). "
            f"Commission will be paid when the buyer approves the purchase or after 7 days."
        )

        NotificationService.send_email(
            to_email=representative.email,
            subject=f"You earned ${reservation.commission_amount} commission!",
            message=message
        )

        logger.info(
            f"Representative {representative.username} earned ${reservation.commission_amount} "
            f"commission on reservation {reservation.id}"
        )

        return f"Notified representative {representative.username} about sale"

    except Reservation.DoesNotExist:
        logger.error(f"Reservation {reservation_id} not found")
        return None
    except Representative.DoesNotExist:
        logger.error(f"Representative assignment {representative_assignment_id} not found")
        return None
    except Exception as e:
        logger.error(f"Error notifying representative: {str(e)}")
        return None


@shared_task
def send_reservation_created_notification(reservation_id, owner_id):
    """
    Notify item owner that a reservation was created
    """
    from .models import Reservation, User
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer', 'representative'
        ).get(id=reservation_id)

        owner = User.objects.get(id=owner_id)

        if reservation.representative:
            message = (
                f"New reservation created by {reservation.buyer.username} "
                f"through representative {reservation.representative.username} "
                f"for your item '{reservation.item.title}'. "
                f"Quantity: {reservation.quantity}, "
                f"Total: ${reservation.total_price}, "
                f"Representative commission: ${reservation.commission_amount}. "
                f"Reservation ID: #{reservation.id}"
            )
            subject = f"New Reservation (via Representative) - {reservation.item.title}"
        else:
            message = (
                f"New reservation created by {reservation.buyer.username} "
                f"for your item '{reservation.item.title}'. "
                f"Quantity: {reservation.quantity}, "
                f"Total: ${reservation.total_price}. "
                f"Reservation ID: #{reservation.id}"
            )
            subject = f"New Reservation - {reservation.item.title}"

        NotificationService.send_email(
            to_email=owner.email,
            subject=subject,
            message=message
        )

        logger.info(f"Notified owner {owner.username} about reservation {reservation.id}")

        return f"Notified owner {owner.username} about reservation"

    except Reservation.DoesNotExist:
        logger.error(f"Reservation {reservation_id} not found")
        return None
    except User.DoesNotExist:
        logger.error(f"Owner {owner_id} not found")
        return None
    except Exception as e:
        logger.error(f"Error notifying owner: {str(e)}")
        return None


@shared_task
def send_commission_paid_notification(reservation_id, representative_id):
    """
    Notify representative that their commission has been paid
    """
    from .models import Reservation, User
    from .notifications import NotificationService

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer'
        ).get(id=reservation_id)

        representative = User.objects.get(id=representative_id)

        message = (
            f"Your commission has been paid! "
            f"Amount: ${reservation.commission_amount} "
            f"for the sale of '{reservation.item.title}' to {reservation.buyer.username}. "
            f"Reservation ID: #{reservation.id}. "
            f"The commission is now available in your account."
        )

        NotificationService.send_email(
            to_email=representative.email,
            subject=f"Commission Payment: ${reservation.commission_amount}",
            message=message
        )

        logger.info(
            f"Commission of ${reservation.commission_amount} paid to "
            f"representative {representative.username} for reservation {reservation.id}"
        )

        return f"Notified representative {representative.username} about commission payment"

    except Reservation.DoesNotExist:
        logger.error(f"Reservation {reservation_id} not found")
        return None
    except User.DoesNotExist:
        logger.error(f"Representative {representative_id} not found")
        return None
    except Exception as e:
        logger.error(f"Error notifying representative: {str(e)}")
        return None

