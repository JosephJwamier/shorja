"""
ReSell Trust - Notifications System
Handles email, SMS, and push notifications (FCM)
"""

from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.html import strip_tags
import logging

logger = logging.getLogger(__name__)

# SMS Support (Twilio)
try:
    from twilio.rest import Client

    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    logger.warning("Twilio not installed. SMS notifications disabled.")

# Firebase FCM push notifications
from .firebase import send_push_notification, FIREBASE_AVAILABLE


class NotificationService:
    """Central notification service"""

    @staticmethod
    def send_email(to_email, subject, message, html_message=None, from_email=None):
        """
        Send email notification
        """
        try:
            from_email = from_email or settings.DEFAULT_FROM_EMAIL

            if html_message:
                email = EmailMultiAlternatives(
                    subject=subject,
                    body=message,
                    from_email=from_email,
                    to=[to_email]
                )
                email.attach_alternative(html_message, "text/html")
                email.send()
            else:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=from_email,
                    recipient_list=[to_email],
                    fail_silently=False,
                )

            logger.info(f"Email sent to {to_email}: {subject}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {str(e)}")
            return False

    @staticmethod
    def send_sms(to_phone, message):
        """
        Send SMS notification via Twilio
        """
        if not TWILIO_AVAILABLE:
            logger.warning(f"SMS not sent to {to_phone}: Twilio not configured")
            return False

        try:
            client = Client(
                settings.TWILIO_ACCOUNT_SID,
                settings.TWILIO_AUTH_TOKEN
            )

            message = client.messages.create(
                body=message,
                from_=settings.TWILIO_PHONE_NUMBER,
                to=to_phone
            )

            logger.info(f"SMS sent to {to_phone}: {message.sid}")
            return True

        except Exception as e:
            logger.error(f"Failed to send SMS to {to_phone}: {str(e)}")
            return False

    @staticmethod
    def send_template_email(to_email, template_name, context, subject):
        """
        Send email using Django template
        """
        try:
            html_message = render_to_string(f'emails/{template_name}.html', context)
            plain_message = strip_tags(html_message)

            return NotificationService.send_email(
                to_email=to_email,
                subject=subject,
                message=plain_message,
                html_message=html_message
            )
        except Exception as e:
            logger.error(f"Failed to send template email: {str(e)}")
            return False


def notify_push(user, title, body, data=None):
    """Save notification to DB and send FCM push to user's device"""
    from .models import Notification
    notification_type = (data or {}).get('type', 'general')
    Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        body=body,
        data=data or {},
    )
    if FIREBASE_AVAILABLE and user.fcm_token:
        send_push_notification(user.fcm_token, title, body, data)


# Specific notification functions for different events

def notify_reservation_created(reservation):
    """Notify seller about new reservation and confirm to buyer"""
    seller = reservation.item.owner
    buyer = reservation.buyer

    subject = f"New Reservation: {reservation.item.title}"
    message = f"""
    Hi {seller.username},

    {buyer.username} has reserved your item "{reservation.item.title}".

    Reservation Details:
    - Item: {reservation.item.title}
    - Deposit: ${reservation.deposit_amount}
    - Total Price: ${reservation.total_price}
    - Expires: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    Please ensure the item is ready for the buyer.

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(seller, subject, f"{buyer.username} reserved your item \"{reservation.item.title}\"",
                data={'type': 'reservation_created', 'reservation_id': str(reservation.id)})

    # Notify buyer
    buyer_subject = f"Reservation Confirmed: {reservation.item.title}"
    buyer_message = f"""
    Hi {buyer.username},

    Your reservation for "{reservation.item.title}" has been confirmed!

    Reservation Details:
    - Deposit Paid: ${reservation.deposit_amount}
    - Remaining: ${reservation.total_price - reservation.deposit_amount}
    - Expires: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    Please complete your payment before the expiration time.

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(buyer.email, buyer_subject, buyer_message)
    notify_push(buyer, buyer_subject, f"Your reservation for \"{reservation.item.title}\" is confirmed!",
                data={'type': 'reservation_confirmed', 'reservation_id': str(reservation.id)})


def notify_reservation_cancelled_by_seller(reservation):
    """Notify buyer that the seller cancelled their reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer

    subject = f"تم إلغاء حجزك: {reservation.item.title}"
    message = f"""
    مرحباً {buyer.username}،

    قام البائع {seller.username} بإلغاء حجزك لمنتج "{reservation.item.title}".

    تفاصيل الحجز:
    - المنتج: {reservation.item.title}
    - السعر: ${reservation.total_price}

    سيتم استرداد المبلغ المدفوع إن وجد.

    نعتذر عن الإزعاج.
    فريق سوقي
    """

    NotificationService.send_email(buyer.email, subject, message)
    notify_push(
        buyer,
        subject,
        f'قام البائع بإلغاء حجزك لمنتج "{reservation.item.title}".',
        data={'type': 'reservation_cancelled_by_seller', 'reservation_id': str(reservation.id)}
    )

    if buyer.phone_verified and buyer.phone:
        sms_message = f"تم إلغاء حجزك لمنتج '{reservation.item.title}' من قبل البائع."
        NotificationService.send_sms(buyer.phone, sms_message)


def notify_reservation_cancelled_by_buyer(reservation):
    """Notify seller that the buyer cancelled their reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer

    subject = f"إلغاء حجز لمنتجك: {reservation.item.title}"
    message = f"""
    مرحباً {seller.username}،

    قام المشتري {buyer.username} بإلغاء حجزه لمنتجك "{reservation.item.title}".

    تفاصيل الحجز:
    - المنتج: {reservation.item.title}
    - السعر: ${reservation.total_price}

    أصبح منتجك متاحاً للمشترين الآخرين.

    فريق سوقي
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(
        seller,
        subject,
        f'ألغى {buyer.username} حجزه لمنتجك "{reservation.item.title}".',
        data={'type': 'reservation_cancelled_by_buyer', 'reservation_id': str(reservation.id)}
    )


def notify_reservation_expiring(reservation, hours_remaining):
    """Notify buyer that reservation is expiring soon"""
    buyer = reservation.buyer

    subject = f"Reservation Expiring Soon: {reservation.item.title}"
    message = f"""
    Hi {buyer.username},

    Your reservation for "{reservation.item.title}" will expire in {hours_remaining} hours!

    Please complete your purchase to avoid losing your deposit of ${reservation.deposit_amount}.

    Remaining Amount: ${reservation.total_price - reservation.deposit_amount}
    Expires: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    Login to complete your purchase: {settings.FRONTEND_URL}/reservations/{reservation.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(buyer.email, subject, message)
    notify_push(buyer, subject, f"Your reservation expires in {hours_remaining} hours. Complete your purchase now!",
                data={'type': 'reservation_expiring', 'reservation_id': str(reservation.id)})

    if buyer.phone_verified and buyer.phone:
        sms_message = f"Reminder: Your reservation for '{reservation.item.title}' expires in {hours_remaining} hours. Complete your purchase now!"
        NotificationService.send_sms(buyer.phone, sms_message)


def notify_reservation_completed(reservation):
    """Notify seller and buyer when buyer completes a reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer

    # --- Notify seller ---
    seller_subject = f"تم شراء منتجك: {reservation.item.title}"
    seller_message = f"""
    مرحباً {seller.username}،

    تم اكمال الحجز وشراء منتجك "{reservation.item.title}" بواسطة {buyer.username}.

    تفاصيل البيع:
    - السعر النهائي: ${reservation.total_price}
    - المشتري: {buyer.username}
    - وقت الاكمال: {reservation.completed_at.strftime('%Y-%m-%d %H:%M')}

    يرجى التواصل مع المشتري لترتيب الاستلام.

    فريق سوقي
    """
    NotificationService.send_email(seller.email, seller_subject, seller_message)
    notify_push(
        seller,
        seller_subject,
        f'أكمل {buyer.username} شراء منتجك "{reservation.item.title}" بمبلغ ${reservation.total_price}.',
        data={'type': 'reservation_completed', 'reservation_id': str(reservation.id)},
    )

    # --- Notify buyer ---
    buyer_subject = f"تم تأكيد شرائك: {reservation.item.title}"
    buyer_message = f"""
    مرحباً {buyer.username}،

    تم تأكيد عملية الشراء بنجاح لمنتج "{reservation.item.title}".

    تفاصيل الشراء:
    - المبلغ المدفوع: ${reservation.total_price}
    - البائع: {seller.username}
    - وقت الاكمال: {reservation.completed_at.strftime('%Y-%m-%d %H:%M')}

    يرجى التواصل مع البائع لترتيب الاستلام.

    فريق سوقي
    """
    NotificationService.send_email(buyer.email, buyer_subject, buyer_message)
    notify_push(
        buyer,
        buyer_subject,
        f'تم تأكيد شرائك لـ "{reservation.item.title}" بمبلغ ${reservation.total_price}.',
        data={'type': 'reservation_completed', 'reservation_id': str(reservation.id)},
    )


def notify_new_bid(auction, bid):
    """Notify about new bid on auction"""
    seller = auction.item.owner

    subject = f"New Bid: {auction.item.title}"
    message = f"""
    Hi {seller.username},

    A new bid has been placed on your auction "{auction.item.title}".

    Bid Details:
    - Bidder: {bid.bidder.username}
    - Amount: ${bid.amount}
    - Previous Price: ${auction.current_price}

    View auction: {settings.FRONTEND_URL}/auctions/{auction.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(seller, subject, f"{bid.bidder.username} bid ${bid.amount} on \"{auction.item.title}\"",
                data={'type': 'new_bid', 'auction_id': str(auction.id), 'bid_amount': str(bid.amount)})

    # Notify previous highest bidder they've been outbid
    previous_winning_bids = auction.bids.filter(
        is_winning=True
    ).exclude(id=bid.id)

    for prev_bid in previous_winning_bids:
        outbid_subject = f"Outbid Alert: {auction.item.title}"
        outbid_message = f"""
        Hi {prev_bid.bidder.username},

        You've been outbid on "{auction.item.title}".

        Your bid: ${prev_bid.amount}
        New highest bid: ${bid.amount}

        Place a higher bid: {settings.FRONTEND_URL}/auctions/{auction.id}

        Best regards,
        ReSell Trust Team
        """
        NotificationService.send_email(prev_bid.bidder.email, outbid_subject, outbid_message)
        notify_push(prev_bid.bidder, outbid_subject, f"You've been outbid on \"{auction.item.title}\". New high: ${bid.amount}",
                    data={'type': 'outbid', 'auction_id': str(auction.id)})


def notify_auction_ending_soon(auction, minutes_remaining):
    """Notify bidders that auction is ending soon"""
    bidders = auction.bids.values_list('bidder', flat=True).distinct()

    from django.contrib.auth import get_user_model
    User = get_user_model()

    for user_id in bidders:
        try:
            user = User.objects.get(id=user_id)

            subject = f"Auction Ending Soon: {auction.item.title}"
            message = f"""
            Hi {user.username},

            The auction for "{auction.item.title}" ends in {minutes_remaining} minutes!

            Current Price: ${auction.current_price}
            Minimum Increment: ${auction.minimum_increment}

            Place your final bid: {settings.FRONTEND_URL}/auctions/{auction.id}

            Best regards,
            ReSell Trust Team
            """

            NotificationService.send_email(user.email, subject, message)

        except User.DoesNotExist:
            continue


def notify_auction_won(auction, reservation):
    """
    Notify the winner that they need to complete their reservation — either
    pay the remaining balance now, or keep the bid deposit already paid and
    settle the rest in cash at delivery. `reservation` is the Reservation
    just created for them by auction_utils.create_winner_reservation.
    """
    winner = reservation.buyer
    remaining = reservation.total_price - reservation.deposit_amount

    subject = f"Congratulations! You Won: {auction.item.title}"
    message = f"""
    Hi {winner.username},

    Congratulations! You won the auction for "{auction.item.title}" at ${reservation.total_price}.

    You already paid a bid deposit of ${reservation.deposit_amount}. Choose how to pay the rest:
    1. Pay the remaining ${remaining} now, then arrange delivery
    2. Keep your deposit as-is and pay the remaining ${remaining} in cash at delivery

    Please choose within 48 hours — otherwise we'll default to option 2 automatically.

    Complete your reservation: {settings.FRONTEND_URL}/reservations/{reservation.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(winner.email, subject, message)
    notify_push(
        winner, subject,
        f"فزت بمزاد \"{auction.item.title}\" بمبلغ ${reservation.total_price}! أكمل حجزك لاختيار طريقة الدفع.",
        data={
            'type': 'auction_won',
            'auction_id': str(auction.id),
            'reservation_id': str(reservation.id),
        }
    )

    if winner.phone_verified and winner.phone:
        sms_message = f"Congratulations! You won the auction for '{auction.item.title}' at ${reservation.total_price}. Complete your reservation now!"
        NotificationService.send_sms(winner.phone, sms_message)

    # Notify seller
    seller = auction.item.owner
    seller_subject = f"Auction Won: {auction.item.title}"
    seller_message = f"""
    Hi {seller.username},

    Your auction for "{auction.item.title}" has ended!

    Winner: {winner.username}
    Final Price: ${reservation.total_price}

    The buyer will complete their reservation soon (paying in full now, or the rest in cash at
    delivery). Please prepare the item for delivery.

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(seller.email, seller_subject, seller_message)


def notify_auction_no_winner(auction):
    """Notify seller that auction ended with no winner"""
    seller = auction.item.owner

    subject = f"Auction Ended: {auction.item.title}"
    message = f"""
    Hi {seller.username},

    Your auction for "{auction.item.title}" has ended without any winning bids.

    You can:
    1. Create a new auction with a lower starting price
    2. List it as a fixed price item
    3. Keep it in your store

    Manage your item: {settings.FRONTEND_URL}/items/{auction.item.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(seller.email, subject, message)


def notify_representative_assigned(representative):
    """Notify representative about assignment"""
    subject = f"Representative Request: {representative.item.title}"
    message = f"""
    Hi {representative.representative.username},

    {representative.owner.username} wants you to be a representative for "{representative.item.title}".

    Commission: {representative.commission_percentage}%

    Permissions:
    - Can List: {'Yes' if representative.can_list else 'No'}
    - Can Negotiate: {'Yes' if representative.can_negotiate else 'No'}
    - Can Complete Sale: {'Yes' if representative.can_complete_sale else 'No'}

    Accept or decline: {settings.FRONTEND_URL}/representatives/{representative.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(representative.representative.email, subject, message)


def notify_new_message(message):
    """Notify user about new chat message"""
    recipient = message.chat.seller if message.sender == message.chat.buyer else message.chat.buyer

    # A chat may be about an item, or a direct conversation with a store.
    chat = message.chat
    about = chat.item.title if chat.item else (chat.store.name if chat.store else 'your store')

    subject = f"New Message: {about}"
    email_message = f"""
    Hi {recipient.username},

    You have a new message from {message.sender.username} about "{about}".

    Message: {message.content[:100]}{'...' if len(message.content) > 100 else ''}

    View chat: {settings.FRONTEND_URL}/chats/{message.chat.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(recipient.email, subject, email_message)
    notify_push(recipient, subject, f"New message from {message.sender.username}: {message.content[:80]}",
                data={'type': 'new_message', 'chat_id': str(message.chat.id)})


def notify_dispute_created(dispute):
    """Notify admin and defendant about new dispute"""
    # Notify defendant
    subject = f"Dispute Filed: {dispute.title}"
    message = f"""
    Hi {dispute.defendant.username},

    A dispute has been filed against you by {dispute.complainant.username}.

    Item: {dispute.item.title}
    Issue: {dispute.description}

    Please respond: {settings.FRONTEND_URL}/disputes/{dispute.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(dispute.defendant.email, subject, message)

    # Notify admin (you can have a list of admin emails)
    admin_subject = f"New Dispute: {dispute.title}"
    admin_message = f"""
    New dispute filed:

    Complainant: {dispute.complainant.username}
    Defendant: {dispute.defendant.username}
    Item: {dispute.item.title}

    Review dispute: {settings.FRONTEND_URL}/disputes/{dispute.id}
    """

    # Send to all admin emails, if any are configured
    for admin_email in getattr(settings, 'ADMIN_EMAILS', []):
        NotificationService.send_email(admin_email, admin_subject, admin_message)


def notify_delivery_assigned(delivery_order):
    """Notify the delivery company's account that a new order was handed to them"""
    company = delivery_order.company
    if not company or not company.account:
        return

    account = company.account
    subject = f"طلب توصيل جديد #{delivery_order.id}"
    body = (
        f'استلام من: {delivery_order.from_name} ({delivery_order.from_phone})\n'
        f'تسليم إلى: {delivery_order.to_name} ({delivery_order.to_phone})\n'
        f'المنتج: {delivery_order.item_summary}'
    )
    notify_push(
        account,
        subject,
        body,
        data={
            'type': 'delivery_assigned',
            'delivery_order_id': str(delivery_order.id),
            'reservation_id': str(delivery_order.reservation_id),
            'direction': delivery_order.direction,
        }
    )


def notify_delivery_arrived(delivery_order):
    """Tell the buyer their order has arrived — they must confirm or refuse receipt"""
    buyer = delivery_order.reservation.buyer
    subject = "وصل طلبك — اضغط تم الاستلام"
    body = f'وصل "{delivery_order.item_summary}". اضغط "تم الاستلام" إذا استلمته بحالة جيدة، أو "رفض الاستلام" إذا كانت هناك مشكلة.'

    notify_push(
        buyer,
        subject,
        body,
        data={
            'type': 'delivery_arrived',
            'delivery_order_id': str(delivery_order.id),
            'reservation_id': str(delivery_order.reservation_id),
        }
    )

    if buyer.phone_verified and buyer.phone:
        NotificationService.send_sms(buyer.phone, body)


def notify_delivery_refused(delivery_order):
    """Notify seller and delivery company that the buyer refused the delivery"""
    reservation = delivery_order.reservation
    seller = reservation.item.owner

    notify_push(
        seller,
        "رفض المشتري استلام الطلب",
        f'رفض {reservation.buyer.username} استلام "{delivery_order.item_summary}". سيُعاد المنتج إليك عبر شركة التوصيل.',
        data={
            'type': 'delivery_refused',
            'delivery_order_id': str(delivery_order.id),
            'reservation_id': str(reservation.id),
        }
    )
    notify_delivery_assigned(delivery_order)


def notify_return_pickup_assigned(delivery_order):
    """Notify the buyer a return pickup for a disputed/refused item was scheduled"""
    buyer = delivery_order.reservation.buyer
    notify_push(
        buyer,
        "جدولة استلام إرجاع المنتج",
        f'ستتواصل شركة التوصيل معك لاستلام "{delivery_order.item_summary}" منك. سيُعاد مبلغك بعد استلامهم للمنتج.',
        data={
            'type': 'delivery_return_scheduled',
            'delivery_order_id': str(delivery_order.id),
            'reservation_id': str(delivery_order.reservation_id),
        }
    )
    notify_delivery_assigned(delivery_order)


def notify_return_refund_released(reservation, amount):
    """Notify buyer their refund was released after the company confirmed the item's return"""
    buyer = reservation.buyer
    subject = "تم استرداد مبلغك"
    body = f'استلمت شركة التوصيل "{reservation.item.title}" منك، وتم إعادة {amount} إلى محفظتك.'

    notify_push(
        buyer,
        subject,
        body,
        data={'type': 'refund_processed', 'reservation_id': str(reservation.id)}
    )
    if buyer.phone_verified and buyer.phone:
        NotificationService.send_sms(buyer.phone, body)


def notify_cash_collected(delivery_order):
    """Notify seller the delivery company collected the COD remainder — it now counts toward their releasable amount"""
    reservation = delivery_order.reservation
    seller = reservation.item.owner
    subject = "تم تحصيل الباقي نقداً"
    body = (
        f'حصّلت شركة التوصيل {delivery_order.cash_to_collect} من {reservation.buyer.username} '
        f'مقابل "{reservation.item.title}" — أصبح المبلغ كاملاً قابلاً للتحرير لك.'
    )
    notify_push(
        seller,
        subject,
        body,
        data={
            'type': 'cash_collected',
            'delivery_order_id': str(delivery_order.id),
            'reservation_id': str(reservation.id),
        }
    )


def notify_payment_successful(transaction):
    """Notify user about successful payment"""
    subject = "Payment Successful"
    message = f"""
    Hi {transaction.user.username},

    Your payment of ${transaction.amount} has been processed successfully.

    Transaction Type: {transaction.get_transaction_type_display()}
    Item: {transaction.item.title if transaction.item else 'N/A'}

    View transaction: {settings.FRONTEND_URL}/transactions/{transaction.id}

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_payment_failed(transaction):
    """Notify user about failed payment"""
    subject = "Payment Failed"
    message = f"""
    Hi {transaction.user.username},

    Your payment of ${transaction.amount} has failed.

    Please update your payment method and try again.

    Try again: {settings.FRONTEND_URL}/transactions/{transaction.id}/retry

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_refund_processed(transaction):
    """Notify user about refund"""
    subject = "Refund Processed"
    message = f"""
    Hi {transaction.user.username},

    A refund of ${transaction.amount} has been processed to your account.

    Transaction Type: {transaction.get_transaction_type_display()}
    Item: {transaction.item.title if transaction.item else 'N/A'}

    The refund should appear in your account within 5-10 business days.

    Best regards,
    ReSell Trust Team
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_live_stream_started(session):
    """Notify a store's followers that it has just gone live (push only — real-time, no email)"""
    from .models import StoreFollow

    follows = StoreFollow.objects.filter(store=session.store).select_related('follower')
    for follow in follows:
        notify_push(
            follow.follower,
            f'{session.store.name} يبث الآن مباشرة!',
            f'"{session.title}" بدأ الآن — انضم قبل فوات الأوان.',
            data={
                'type': 'live_stream_started',
                'live_session_id': str(session.id),
                'store_id': str(session.store_id),
            }
        )


def notify_live_item_reserved(reservation):
    """Notify the item's owner that a viewer reserved it during a live session"""
    owner = reservation.item.owner
    notify_push(
        owner,
        'تم حجز منتجك أثناء البث المباشر',
        f'{reservation.buyer.username} حجز "{reservation.item.title}" بعربون {reservation.deposit_amount}.',
        data={
            'type': 'live_item_reserved',
            'reservation_id': str(reservation.id),
            'live_session_id': str(reservation.live_session_id),
            'item_id': str(reservation.item_id),
        }
    )


def notify_live_item_sold_out(queue_item):
    """Notify the item's owner that a live-session queue item has sold out"""
    owner = queue_item.item.owner
    notify_push(
        owner,
        'نفدت الكمية',
        f'نفدت كمية "{queue_item.item.title}" أثناء البث المباشر.',
        data={
            'type': 'live_item_sold_out',
            'live_session_id': str(queue_item.session_id),
            'item_id': str(queue_item.item_id),
        }
    )


# Utility function for bulk notifications
def send_bulk_notification(user_ids, subject, message, notification_type='email'):
    """
    Send notification to multiple users
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    users = User.objects.filter(id__in=user_ids)

    for user in users:
        if notification_type == 'email':
            NotificationService.send_email(user.email, subject, message)
        elif notification_type == 'sms' and user.phone_verified:
            NotificationService.send_sms(user.phone, message)