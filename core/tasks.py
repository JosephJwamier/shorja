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
    from .models import ItemVariant, Reservation, Transaction

    now = timezone.now()
    expired_ids = list(Reservation.objects.filter(
        status='pending',
        expires_at__lt=now,
        auction__isnull=True
    ).values_list('id', flat=True))

    processed_count = 0
    for reservation_id in expired_ids:
        try:
            with transaction.atomic():
                reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
                if reservation.status != 'pending':
                    continue  # completed or cancelled since the list was built
                reservation.status = 'expired'
                reservation.save(update_fields=['status'])

                # A represented sale is the one kind that takes stock at
                # creation (see ReservationViewSet.create_representative_
                # reservation) and books a pending commission — both must be
                # undone here or the stock leaks on every expiry.
                if reservation.representative_id:
                    variant = reservation.stock_variant
                    if variant is not None:
                        variant = ItemVariant.objects.select_for_update().get(pk=variant.pk)
                        variant.return_stock(reservation.quantity)
                    Transaction.objects.filter(
                        reservation=reservation, transaction_type='commission', status='pending'
                    ).update(status='cancelled')

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
def notify_store_followers_reel(reel_id):
    """Fan out the 'store just posted a reel' push to all its followers. Run
    via .delay() from ReelViewSet.finalize so a store with many followers
    doesn't block the HTTP response."""
    from .models import Reel
    from .notifications import notify_reel_posted

    try:
        reel = Reel.objects.select_related('store').get(id=reel_id)
        notify_reel_posted(reel)
        return f"Notified followers of store {reel.store_id} about reel {reel_id}"
    except Reel.DoesNotExist:
        logger.error(f"Reel {reel_id} not found")
        return f"Reel {reel_id} not found"


@shared_task
def cleanup_orphaned_reel_uploads():
    """
    Safety net for an upload flow abandoned mid-way (app closed after
    request_upload_url but before finalize, network drop mid-PUT, etc).
    Deletes Reel rows still 'pending_upload' past
    REEL_PENDING_UPLOAD_CLEANUP_HOURS, along with whatever they may have
    already put in the bucket under the incoming/ prefix — mirrors the
    bucket's lifecycle rule on incoming/* (24h; R2 supports the same rule
    S3 did) as a second, faster line of defense that also cleans up the
    abandoned DB row itself.
    """
    from django.conf import settings
    from . import aws_utils
    from .models import Reel

    cutoff = timezone.now() - timedelta(
        hours=getattr(settings, 'REEL_PENDING_UPLOAD_CLEANUP_HOURS', 2)
    )
    stale_reels = Reel.objects.filter(status='pending_upload', created_at__lt=cutoff)

    deleted_count = 0
    for reel in stale_reels:
        try:
            if reel.video_key:
                aws_utils.delete_object(reel.video_key)
            if reel.thumbnail_key:
                aws_utils.delete_object(reel.thumbnail_key)
            reel.delete()
            deleted_count += 1
        except Exception as e:
            logger.error(f"Error cleaning up orphaned reel upload {reel.id}: {str(e)}")

    return f"Cleaned up {deleted_count} orphaned reel uploads"


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
    Release payments to sellers once each reservation's item-specific
    warranty period has passed without disputes or buyer approval
    - Check reservations whose outbound delivery reached the buyer at least
      the shortest possible warranty period ago (1 day) — can_release_payment()
      below then re-checks the exact per-item warranty against
      Reservation.received_at (the buyer's actual receipt date, not
      completed_at/payment date — a reservation the seller hasn't shipped
      yet, or that's still in transit, must never age into 'confirmed' here)
    - Verify no open disputes
    - Release payment to seller (minus commission if applicable)
    - Release commission to representative if applicable
    """
    from .models import Reservation, Dispute
    from .wallet_utils import release_reservation_payment

    # Coarse pre-filter using the shortest possible warranty (1 day) so the
    # query doesn't scan reservations that can't possibly be eligible under
    # any warranty length; can_release_payment() supplies the precise,
    # per-reservation answer below.
    min_possible_warranty_ago = timezone.now() - timedelta(days=1)

    reservations = Reservation.objects.filter(
        status__in=['completed', 'confirmed'],
        payment_released_to_seller=False,
        buyer_approved=False,
        delivery_orders__direction='outbound',
        delivery_orders__status__in=['delivered', 'confirmed'],
        delivery_orders__delivered_at__lte=min_possible_warranty_ago,
    ).select_related('item__owner', 'representative').distinct()

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
                    # Warranty period passed with no dispute — the sale is settled.
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
    from .auction_utils import (
        create_winner_reservation, broadcast_live_auction_ended, refund_losing_deposits,
    )

    now = timezone.now()
    ended_ids = list(AuctionListing.objects.filter(
        status='active', end_time__lt=now
    ).values_list('id', flat=True))

    processed_count = 0
    for auction_id in ended_ids:
        try:
            reservation = None
            with transaction.atomic():
                # Lock and re-read: end_early or a second worker may have
                # settled this auction since the list above was built, and
                # settling twice would refund deposits twice.
                auction = AuctionListing.objects.select_for_update().select_related(
                    'item__owner').get(pk=auction_id)
                if auction.status != 'active':
                    continue

                winning_bid = auction.bids.filter(is_winning=True).first()

                if winning_bid:
                    refund_losing_deposits(auction, winning_bid)
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
def recompute_store_levels():
    """Nightly: re-derive every active store's performance level
    (برونزي/فضّي/ذهبي/ماسي) from its last-90-day confirmed sales, rating and
    followers, and keep the automatic `verified` badge in step with the
    owner's verification flags. Levels are derived, so this is the only
    writer of UserStore.level."""
    from .models import UserStore
    from .services.store_badges import recompute_level, sync_verified_badge

    changed = 0
    for store in UserStore.objects.filter(is_active=True).select_related('user'):
        try:
            old, new = recompute_level(store)
            sync_verified_badge(store)
            if old != new:
                changed += 1
                logger.info("store %s level %s -> %s", store.id, old or '-', new or '-')
        except Exception as exc:  # one bad store must not stop the sweep
            logger.error("level recompute failed for store %s: %s", store.id, exc)
    return "Recomputed levels; %d changed" % changed


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
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        if reservation.status == 'pending':
            time_remaining = reservation.expires_at - timezone.now()
            hours_remaining = int(time_remaining.total_seconds() / 3600)

            message = (
                f"تذكير: حجزك لمنتج '{bidi_isolate(reservation.item.title)}' "
                f"ينتهي خلال {hours_remaining} ساعة. "
                f"يرجى إتمام عملية الشراء لتجنب خسارة حجزك."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="حجزك على وشك الانتهاء",
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
    from .notifications import NotificationService, bidi_isolate, format_days_ar

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        seller = reservation.item.owner
        item_title = bidi_isolate(reservation.item.title)

        if release_type == 'auto_release':
            subject = f"تم تحرير الدفعة - مرور {format_days_ar(reservation.warranty_days)}"
            message = (
                f"تم تحرير دفعة بمبلغ {reservation.total_price} IQD لمنتج '{item_title}' "
                f"إليك تلقائياً بعد مرور {format_days_ar(reservation.warranty_days)} دون أي نزاع."
            )
        elif release_type == 'buyer_approved':
            subject = "تم تحرير الدفعة - موافقة المشتري"
            message = (
                f"تم تحرير دفعة بمبلغ {reservation.total_price} IQD لمنتج '{item_title}' "
                f"بعد موافقة المشتري."
            )
        else:  # admin_approved
            subject = "تم تحرير الدفعة - موافقة الإدارة"
            message = (
                f"تم تحرير دفعة بمبلغ {reservation.total_price} IQD لمنتج '{item_title}' "
                f"بعد قرار الإدارة في النزاع."
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
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        if reservation.status == 'pending' and not reservation.seller_cancellation_approved:
            message = (
                f"انتهت مهلة الإلغاء المجاني (ساعتان) لحجز منتج '{bidi_isolate(reservation.item.title)}'. "
                f"لم يعد بإمكانك إلغاء هذا الحجز دون موافقة البائع. "
                f"يرجى التواصل مع البائع إذا أردت الإلغاء، أو فتح نزاع إذا كانت هناك مشكلة."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="انتهت مهلة الإلغاء",
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
    from .notifications import NotificationService, bidi_isolate, format_days_ar

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        received_at = reservation.received_at
        if reservation.status == 'completed' and not reservation.buyer_approved and received_at:
            days_since_received = (timezone.now() - received_at).days
            days_remaining = reservation.warranty_days - days_since_received

            message = (
                f"استلمت منتج '{bidi_isolate(reservation.item.title)}' منذ {days_since_received} يوم(أيام). "
                f"إذا كنت راضياً عن عملية الشراء، يرجى الموافقة عليها لتحرير المبلغ للبائع. "
                f"سيتم تحرير المبلغ تلقائياً خلال {format_days_ar(max(0, days_remaining))} إذا لم يُفتح أي نزاع."
            )

            NotificationService.send_email(
                to_email=reservation.buyer.email,
                subject="يرجى تقييم عملية شرائك",
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
    from .notifications import NotificationService, bidi_isolate

    try:
        dispute = Dispute.objects.get(id=dispute_id)
        item_title = bidi_isolate(dispute.item.title)

        if notification_type == 'created':
            # Notify defendant
            message = (
                f"تم فتح نزاع ضدك بخصوص '{item_title}'. "
                f"العنوان: {bidi_isolate(dispute.title)}. "
                f"يرجى مراجعة لوحة التحكم للتفاصيل."
            )
            NotificationService.send_email(
                to_email=dispute.defendant.email,
                subject="تم فتح نزاع",
                message=message
            )

        elif notification_type == 'resolved':
            # Notify both parties
            complainant_message = (
                f"تم حل النزاع الخاص بـ '{item_title}'. "
                f"القرار: {dispute.resolution}"
            )
            defendant_message = (
                f"تم حل النزاع المفتوح ضدك بخصوص '{item_title}'. "
                f"القرار: {dispute.resolution}"
            )

            NotificationService.send_email(
                to_email=dispute.complainant.email,
                subject="تم حل النزاع",
                message=complainant_message
            )
            NotificationService.send_email(
                to_email=dispute.defendant.email,
                subject="تم حل النزاع",
                message=defendant_message
            )

        elif notification_type == 'investigating':
            # Notify complainant
            message = (
                f"نزاعك بخصوص '{item_title}' قيد المراجعة الآن. "
                f"سنُبلغك فور اتخاذ قرار."
            )
            NotificationService.send_email(
                to_email=dispute.complainant.email,
                subject="النزاع قيد المراجعة",
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
    from .notifications import NotificationService, bidi_isolate

    try:
        auction = AuctionListing.objects.get(id=auction_id)

        if auction.status == 'active':
            time_remaining = auction.end_time - timezone.now()
            minutes_remaining = int(time_remaining.total_seconds() / 60)

            # Notify all bidders
            bidders = auction.bids.values_list('bidder__email', flat=True).distinct()

            message = (
                f"المزاد على '{bidi_isolate(auction.item.title)}' ينتهي خلال {minutes_remaining} دقيقة! "
                f"السعر الحالي: {auction.current_price} IQD"
            )

            for email in bidders:
                NotificationService.send_email(
                    to_email=email,
                    subject="المزاد ينتهي قريباً",
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
def decay_category_affinities(factor=0.9):
    """
    Weekly multiplicative decay on every UserCategoryAffinity.score.

    record_interest() (see models.py) only ever adds — a purchase, favorite,
    or store-follow bumps a category's score but nothing ever reduces it.
    Without this task a customer's interests from months ago stay stuck at
    whatever they reached and can keep outranking a genuinely new interest
    in `recommended` indefinitely, since the new one has to out-accumulate
    the old rather than the old one fading. factor=0.9 run weekly gives
    roughly a 6-7 week half-life — old signal fades over a month or two
    instead of vanishing overnight or lingering forever.

    Rows that decay to 0 are deleted outright rather than left at 0 — a
    zero-score affinity is equivalent to no affinity (top_category_ids
    already filters score__gt=0) and leaving it around just grows the table.

    Uses Floor rather than Cast(...AS SIGNED): MySQL's integer CAST rounds
    to nearest rather than truncating, so a score of 1 would decay to
    round(0.9) == 1 forever and never actually reach 0.
    """
    from django.db.models import F, IntegerField
    from django.db.models.functions import Floor
    from .models import UserCategoryAffinity

    decayed = UserCategoryAffinity.objects.filter(score__gt=0).update(
        score=Floor(F('score') * factor, output_field=IntegerField())
    )
    zeroed_out, _ = UserCategoryAffinity.objects.filter(score=0).delete()

    logger.info(
        f"decay_category_affinities: decayed {decayed} rows, "
        f"removed {zeroed_out} that hit 0 (factor={factor})"
    )
    return f"Decayed {decayed} affinities, removed {zeroed_out} zeroed-out rows"


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
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        representative = Representative.objects.get(id=representative_id)
        item_title = bidi_isolate(representative.item.title)

        message = (
            f"يريد {bidi_isolate(representative.representative.username)} تمثيل منتجك '{item_title}'. "
            f"العمولة: {representative.commission_percentage}%. "
            f"الرسالة: {representative.request_message or 'لا توجد رسالة'}"
        )

        NotificationService.send_email(
            to_email=representative.owner.email,
            subject="طلب تمثيل جديد",
            message=message
        )
        notify_push(
            representative.owner,
            title="طلب تمثيل جديد",
            body=f'يريد {bidi_isolate(representative.representative.username)} تمثيل منتجك "{item_title}".',
            data={'type': 'representative_request', 'representative_id': str(representative.id)},
        )

        logger.info(f"Sent representative request notification for {representative_id}")

    except Exception as e:
        logger.error(f"Error sending representative request notification: {str(e)}")


@shared_task
def send_representative_accepted_notification(representative_id):
    """Notify representative that request was accepted"""
    from .models import Representative
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        representative = Representative.objects.get(id=representative_id)
        item_title = bidi_isolate(representative.item.title)

        message = (
            f"تم قبول طلبك لتمثيل '{item_title}'! "
            f"العمولة: {representative.commission_percentage}%. "
            f"يمكنك الآن البدء بتمثيل هذا المنتج."
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="تم قبول طلب التمثيل",
            message=message
        )
        notify_push(
            representative.representative,
            title="تم قبول طلب التمثيل",
            body=f'تم قبول طلبك لتمثيل "{item_title}"! العمولة: {representative.commission_percentage}%.',
            data={'type': 'representative_accepted', 'representative_id': str(representative.id)},
        )

        logger.info(f"Sent acceptance notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending acceptance notification: {str(e)}")


@shared_task
def send_representative_rejected_notification(representative_id):
    """Notify representative that request was rejected"""
    from .models import Representative
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        representative = Representative.objects.get(id=representative_id)
        item_title = bidi_isolate(representative.item.title)

        message = (
            f"تم رفض طلبك لتمثيل '{item_title}'. "
            f"{f'السبب: {representative.rejection_reason}' if representative.rejection_reason else ''}"
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="تم رفض طلب التمثيل",
            message=message
        )
        notify_push(
            representative.representative,
            title="تم رفض طلب التمثيل",
            body=f'تم رفض طلبك لتمثيل "{item_title}".'
                 + (f' السبب: {representative.rejection_reason}' if representative.rejection_reason else ''),
            data={'type': 'representative_rejected', 'representative_id': str(representative.id)},
        )

        logger.info(f"Sent rejection notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending rejection notification: {str(e)}")


@shared_task
def send_representative_cancelled_notification(representative_id, cancelled_by_user_id):
    """Notify party about cancellation"""
    from .models import Representative, User
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        representative = Representative.objects.get(id=representative_id)
        cancelled_by = User.objects.get(id=cancelled_by_user_id)
        item_title = bidi_isolate(representative.item.title)

        # Determine who to notify
        if cancelled_by == representative.owner:
            notify_user = representative.representative
            message = f"ألغى المالك تكليفك بتمثيل '{item_title}'"
        else:
            notify_user = representative.owner
            message = f"ألغى {bidi_isolate(representative.representative.username)} طلبه لتمثيل '{item_title}'"

        NotificationService.send_email(
            to_email=notify_user.email,
            subject="تم إلغاء تكليف التمثيل",
            message=message
        )
        notify_push(
            notify_user,
            title="تم إلغاء تكليف التمثيل",
            body=message,
            data={'type': 'representative_cancelled', 'representative_id': str(representative.id)},
        )

        logger.info(f"Sent cancellation notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation notification: {str(e)}")


@shared_task
def send_representative_permissions_updated_notification(representative_id):
    """Notify representative about permission changes"""
    from .models import Representative
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        representative = Representative.objects.get(id=representative_id)
        item_title = bidi_isolate(representative.item.title)

        message = (
            f"تم تحديث صلاحياتك لتمثيل '{item_title}'. "
            f"يمكنه الإدراج: {'نعم' if representative.can_list else 'لا'}, "
            f"يمكنه التفاوض: {'نعم' if representative.can_negotiate else 'لا'}, "
            f"يمكنه إتمام البيع: {'نعم' if representative.can_complete_sale else 'لا'}, "
            f"العمولة: {representative.commission_percentage}%"
        )

        NotificationService.send_email(
            to_email=representative.representative.email,
            subject="تم تحديث صلاحيات التمثيل",
            message=message
        )
        notify_push(
            representative.representative,
            title="تم تحديث صلاحيات التمثيل",
            body=f'تم تحديث صلاحياتك لتمثيل "{item_title}". العمولة: {representative.commission_percentage}%.',
            data={'type': 'representative_permissions_updated', 'representative_id': str(representative.id)},
        )

        logger.info(f"Sent permissions update notification for representative {representative_id}")

    except Exception as e:
        logger.error(f"Error sending permissions notification: {str(e)}")


@shared_task
def send_payment_release_request_notification(reservation_id, seller_username, buyer_id):
    """Send notification to buyer when seller requests payment release"""
    from .models import Reservation, User
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        buyer = User.objects.get(id=buyer_id)
        item_title = bidi_isolate(reservation.item.title)

        message = (
            f"طلب منك {seller_username} الموافقة على تحرير الدفعة لحجزك المكتمل. "
            f"المنتج: {item_title}، "
            f"المبلغ: {reservation.total_price} IQD. "
            f"إذا كنت راضياً عن العملية، يرجى الموافقة على الحجز لتحرير المبلغ للبائع."
        )

        NotificationService.send_email(
            to_email=buyer.email,
            subject=f"طلب تحرير الدفعة - الحجز رقم #{reservation_id}",
            message=message
        )

        notify_push(
            buyer,
            title='طلب تحرير الدفعة',
            body=f'{bidi_isolate(seller_username)} يطلب تأكيدك على استلام "{item_title}" لتحرير المبلغ له.',
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
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        seller = User.objects.get(id=seller_id)

        message = (
            f"تم تحرير الدفعة الخاصة بالحجز رقم #{reservation_id}. "
            f"المنتج: {bidi_isolate(reservation.item.title)}, "
            f"المبلغ: {reservation.total_price} IQD, "
            f"المشتري: {bidi_isolate(reservation.buyer.username)}. "
            f"أصبح المبلغ متاحاً الآن في حسابك."
        )

        NotificationService.send_email(
            to_email=seller.email,
            subject=f"تم تحرير الدفعة - الحجز رقم #{reservation_id}",
            message=message
        )

        logger.info(f"Payment released notification sent to seller {seller_id} for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending payment released notification: {str(e)}")


@shared_task
def send_cancellation_restriction_notification(reservation_id):
    """Send notification to buyer about cancellation restrictions"""
    from .models import Reservation
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)

        message = (
            f"انتهت مهلة الإلغاء المجاني (ساعتان) لحجزك. "
            f"المنتج: {bidi_isolate(reservation.item.title)}, "
            f"رقم الحجز: #{reservation_id}. "
            f"لم يعد بإمكانك إلغاء هذا الحجز دون موافقة البائع. يرجى إتمام الشراء أو التواصل مع البائع."
        )

        NotificationService.send_email(
            to_email=reservation.buyer.email,
            subject=f"انتهت مهلة الإلغاء - الحجز رقم #{reservation_id}",
            message=message
        )

        logger.info(f"Sent cancellation restriction notice for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending cancellation restriction notification: {str(e)}")


@shared_task
def send_reservation_expired_notification(reservation_id):
    """Send notification when reservation expires"""
    from .models import Reservation
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        item_title = bidi_isolate(reservation.item.title)

        # Notify buyer
        buyer_message = (
            f"انتهى حجزك. "
            f"المنتج: {item_title}, "
            f"رقم الحجز: #{reservation_id}. "
            f"تمت إعادة الكمية المحجوزة إلى المخزون المتاح."
        )

        NotificationService.send_email(
            to_email=reservation.buyer.email,
            subject=f"انتهى الحجز - الحجز رقم #{reservation_id}",
            message=buyer_message
        )

        # Notify seller
        seller_message = (
            f"انتهى حجز لأحد منتجاتك. "
            f"المنتج: {item_title}, "
            f"رقم الحجز: #{reservation_id}, "
            f"المشتري: {bidi_isolate(reservation.buyer.username)}. "
            f"أصبح المنتج متاحاً الآن للمشترين الآخرين."
        )

        NotificationService.send_email(
            to_email=reservation.item.owner.email,
            subject=f"انتهى الحجز - {item_title}",
            message=seller_message
        )

        logger.info(f"Sent expiration notifications for reservation {reservation_id}")

    except Exception as e:
        logger.error(f"Error sending reservation expired notification: {str(e)}")


@shared_task
def send_reservation_cancelled_notification(reservation_id, cancelled_by_user_id):
    """Notify parties about reservation cancellation"""
    from .models import Reservation, User
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.get(id=reservation_id)
        cancelled_by = User.objects.get(id=cancelled_by_user_id)
        item_title = bidi_isolate(reservation.item.title)

        # Determine who to notify
        if cancelled_by == reservation.buyer:
            notify_user = reservation.item.owner
            message = (
                f"ألغى {bidi_isolate(reservation.buyer.username)} حجزه لمنتجك. "
                f"المنتج: {item_title}, "
                f"رقم الحجز: #{reservation_id}, "
                f"الكمية: {reservation.quantity}. "
                f"أصبح المنتج متاحاً الآن للمشترين الآخرين."
            )
        else:
            notify_user = reservation.buyer
            message = (
                f"ألغى البائع حجزك. "
                f"المنتج: {item_title}, "
                f"رقم الحجز: #{reservation_id}. "
                f"سيتم استرداد أي عربون مدفوع."
            )

        NotificationService.send_email(
            to_email=notify_user.email,
            subject=f"تم إلغاء الحجز - الحجز رقم #{reservation_id}",
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
    from .notifications import NotificationService, notify_push, bidi_isolate, format_days_ar

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer', 'representative'
        ).get(id=reservation_id)

        representative_assignment = Representative.objects.select_related(
            'representative', 'item'
        ).get(id=representative_assignment_id)

        representative = representative_assignment.representative
        item_title = bidi_isolate(reservation.item.title)

        # This fires right after the (still 'pending') reservation is
        # created — reservation.warranty_days is only snapshotted at
        # completion, so read the item's current configured value instead.
        message = (
            f"أخبار رائعة! اشترى {bidi_isolate(reservation.buyer.username)} منتج '{item_title}' عبر رابط تمثيلك. "
            f"مبلغ البيع: {reservation.total_price} IQD, "
            f"عمولتك: {reservation.commission_amount} IQD ({representative_assignment.commission_percentage}%). "
            f"ستُدفع العمولة عند موافقة المشتري على الشراء أو بعد {format_days_ar(reservation.item.warranty_days)}."
        )

        NotificationService.send_email(
            to_email=representative.email,
            subject=f"حصلت على عمولة {reservation.commission_amount} IQD!",
            message=message
        )
        notify_push(
            representative,
            title=f"حصلت على عمولة {reservation.commission_amount} IQD!",
            body=f'اشترى {bidi_isolate(reservation.buyer.username)} "{item_title}" عبر رابط تمثيلك.',
            data={
                'type': 'representative_sale',
                'reservation_id': str(reservation.id),
                'representative_id': str(representative_assignment.id),
            },
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
    from .notifications import NotificationService, bidi_isolate

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer', 'representative'
        ).get(id=reservation_id)

        owner = User.objects.get(id=owner_id)
        item_title = bidi_isolate(reservation.item.title)

        if reservation.representative:
            message = (
                f"حجز جديد من {bidi_isolate(reservation.buyer.username)} "
                f"عبر الممثل {bidi_isolate(reservation.representative.username)} "
                f"لمنتجك '{item_title}'. "
                f"الكمية: {reservation.quantity}, "
                f"الإجمالي: {reservation.total_price} IQD, "
                f"عمولة الممثل: {reservation.commission_amount} IQD. "
                f"رقم الحجز: #{reservation.id}"
            )
            subject = f"حجز جديد (عبر ممثل) - {item_title}"
        else:
            message = (
                f"حجز جديد من {bidi_isolate(reservation.buyer.username)} "
                f"لمنتجك '{item_title}'. "
                f"الكمية: {reservation.quantity}, "
                f"الإجمالي: {reservation.total_price} IQD. "
                f"رقم الحجز: #{reservation.id}"
            )
            subject = f"حجز جديد - {item_title}"

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
    from .notifications import NotificationService, notify_push, bidi_isolate

    try:
        reservation = Reservation.objects.select_related(
            'item', 'buyer'
        ).get(id=reservation_id)

        representative = User.objects.get(id=representative_id)
        item_title = bidi_isolate(reservation.item.title)

        message = (
            f"تم دفع عمولتك! "
            f"المبلغ: {reservation.commission_amount} IQD "
            f"مقابل بيع '{item_title}' إلى {bidi_isolate(reservation.buyer.username)}. "
            f"رقم الحجز: #{reservation.id}. "
            f"أصبح المبلغ متاحاً الآن في حسابك."
        )

        NotificationService.send_email(
            to_email=representative.email,
            subject=f"دفعة عمولة: {reservation.commission_amount} IQD",
            message=message
        )
        notify_push(
            representative,
            title=f"دفعة عمولة: {reservation.commission_amount} IQD",
            body=f'تم دفع عمولتك مقابل بيع "{item_title}". أصبح المبلغ متاحاً الآن في حسابك.',
            data={'type': 'representative_commission_paid', 'reservation_id': str(reservation.id)},
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


@shared_task
def send_resale_agreement_request_notification(transfer_id):
    """Notify the item owner about a new wholesale resale agreement request"""
    from .models import ItemTransfer
    from .notifications import notify_push, bidi_isolate

    try:
        transfer = ItemTransfer.objects.select_related('item', 'from_user', 'to_user').get(id=transfer_id)
        item_title = bidi_isolate(transfer.item.title)

        notify_push(
            transfer.from_user,
            title="طلب نقل ملكية بالجملة",
            body=f'يريد {bidi_isolate(transfer.to_user.username)} شراء {transfer.quantity} من "{item_title}" لإعادة البيع.',
            data={'type': 'resale_agreement_request', 'transfer_id': str(transfer.id)},
        )

        logger.info(f"Sent resale agreement request notification for transfer {transfer_id}")

    except Exception as e:
        logger.error(f"Error sending resale agreement request notification: {str(e)}")


@shared_task
def send_resale_agreement_approved_notification(transfer_id):
    """Notify the reseller that the owner approved their resale agreement"""
    from .models import ItemTransfer
    from .notifications import notify_push, bidi_isolate

    try:
        transfer = ItemTransfer.objects.select_related('item', 'from_user', 'to_user').get(id=transfer_id)
        item_title = bidi_isolate(transfer.item.title)

        notify_push(
            transfer.to_user,
            title="تمت الموافقة على اتفاقية إعادة البيع",
            body=f'وافق {bidi_isolate(transfer.from_user.username)} على طلبك. أصبحت الآن تملك {transfer.quantity} من "{item_title}" في متجرك.',
            data={'type': 'resale_agreement_approved', 'transfer_id': str(transfer.id)},
        )

        logger.info(f"Sent resale agreement approval notification for transfer {transfer_id}")

    except Exception as e:
        logger.error(f"Error sending resale agreement approval notification: {str(e)}")


@shared_task
def send_resale_agreement_rejected_notification(transfer_id):
    """Notify the reseller that the owner rejected their resale agreement request"""
    from .models import ItemTransfer
    from .notifications import notify_push, bidi_isolate

    try:
        transfer = ItemTransfer.objects.select_related('item', 'from_user', 'to_user').get(id=transfer_id)
        item_title = bidi_isolate(transfer.item.title)

        notify_push(
            transfer.to_user,
            title="تم رفض اتفاقية إعادة البيع",
            body=f'رفض {bidi_isolate(transfer.from_user.username)} طلبك لإعادة بيع "{item_title}".'
                 + (f' السبب: {transfer.rejection_reason}' if transfer.rejection_reason else ''),
            data={'type': 'resale_agreement_rejected', 'transfer_id': str(transfer.id)},
        )

        logger.info(f"Sent resale agreement rejection notification for transfer {transfer_id}")

    except Exception as e:
        logger.error(f"Error sending resale agreement rejection notification: {str(e)}")


@shared_task
def send_resale_agreement_cancelled_notification(transfer_id, cancelled_by_user_id):
    """Notify the other party that a resale agreement was cancelled/ended"""
    from .models import ItemTransfer, User
    from .notifications import notify_push, bidi_isolate

    try:
        transfer = ItemTransfer.objects.select_related('item', 'from_user', 'to_user').get(id=transfer_id)
        cancelled_by = User.objects.get(id=cancelled_by_user_id)
        item_title = bidi_isolate(transfer.item.title)

        if cancelled_by == transfer.from_user:
            notify_user = transfer.to_user
            body = f'أنهى {bidi_isolate(transfer.from_user.username)} اتفاقية استضافة "{item_title}".'
        else:
            notify_user = transfer.from_user
            body = f'ألغى {bidi_isolate(transfer.to_user.username)} اتفاقية إعادة بيع "{item_title}".'

        notify_push(
            notify_user,
            title="تم إنهاء اتفاقية إعادة البيع",
            body=body,
            data={'type': 'resale_agreement_cancelled', 'transfer_id': str(transfer.id)},
        )

        logger.info(f"Sent resale agreement cancellation notification for transfer {transfer_id}")

    except Exception as e:
        logger.error(f"Error sending resale agreement cancellation notification: {str(e)}")


@shared_task
def purge_archived_items(retention_days=365):
    """
    Permanently deletes Item rows that have sat archived (status='archived',
    see ItemViewSet.destroy / archive_item) for at least `retention_days`.

    Item is CASCADE-referenced by Reservation/ItemTransfer/Representative/
    ProductReview/Chat/LiveSessionItem, and Reservation is in turn
    CASCADE-referenced by Dispute/DeliveryOrder — so this only deletes items
    with no reservation still open (pending/completed/disputed), no dispute
    still open (open/investigating), and no delivery order still in flight
    (assigned/picked_up/delivered). Anything mid-transaction is left alone
    no matter how long ago it was archived; it'll be picked up on a later
    run once it settles into a terminal state.
    """
    from .models import Item

    cutoff_date = timezone.now() - timedelta(days=retention_days)

    candidates = Item.objects.filter(
        status='archived', updated_at__lt=cutoff_date
    ).exclude(
        reservations__status__in=['pending', 'completed', 'disputed']
    ).exclude(
        disputes__status__in=['open', 'investigating']
    ).exclude(
        reservations__delivery_orders__status__in=['assigned', 'picked_up', 'delivered']
    ).distinct()

    item_ids = list(candidates.values_list('id', flat=True))
    if not item_ids:
        logger.info("purge_archived_items: no items eligible for permanent deletion")
        return "No items eligible"

    deleted_count = Item.objects.filter(id__in=item_ids).delete()[0]
    logger.info(f"purge_archived_items: permanently deleted {len(item_ids)} item(s) "
                f"(ids={item_ids}), {deleted_count} row(s) total across related tables")
    return f"Deleted {len(item_ids)} items"

