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

# Firebase FCM push notifications
from .firebase import send_push_notification, FIREBASE_AVAILABLE

# Unicode bidi isolates (Bidi_Control chars, unaffected by GSM-7/UCS-2 concerns
# since Arabic already forces Unicode SMS encoding). FSI picks the embedded
# run's own direction (Arabic or Latin) without letting it drag the
# surrounding Arabic sentence out of order; PDI closes the isolation.
_FSI = '⁨'
_PDI = '⁩'


def bidi_isolate(value):
    """
    Wrap a dynamic value (product title, username, store name, ...) that may
    contain Latin/English text before splicing it into an Arabic sentence.
    Without this, the bidi algorithm can reorder punctuation/words around an
    embedded LTR run (e.g. an English product name) and scramble the Arabic
    text around it. Safe to use even when the value turns out to be Arabic.
    """
    return f'{_FSI}{value}{_PDI}'


def format_days_ar(n):
    """Arabic day-count with correct singular/plural: 1 -> 'يوم واحد', else 'N أيام'."""
    return 'يوم واحد' if n == 1 else f'{n} أيام'


# Arabic labels for Transaction.transaction_type, kept local to this module so
# notification copy can be Arabic without changing Transaction's stored
# choices (which are relied on elsewhere and would need a migration).
_TRANSACTION_TYPE_LABELS_AR = {
    'deposit': 'عربون',
    'payment': 'دفعة',
    'refund': 'استرداد',
    'commission': 'عمولة',
    'penalty': 'غرامة',
    'withdrawal': 'سحب',
    'storage_fee': 'رسوم تخزين',
    'purchase': 'شراء',
    'payout': 'صرف',
    'delivery_fee': 'رسوم توصيل',
}


def _transaction_type_label(transaction):
    return _TRANSACTION_TYPE_LABELS_AR.get(
        transaction.transaction_type, transaction.get_transaction_type_display()
    )


class NotificationService:
    """Central notification service"""

    @staticmethod
    def send_email(to_email, subject, message, html_message=None, from_email=None):
        """
        Send email notification.

        Returns False without sending while settings.EMAIL_NOTIFICATIONS_ENABLED
        is off — the single switch for all outgoing platform mail (see the
        setting's comment). Logged, not silent: the trail says what would have
        gone out, so turning the switch on later is a change in delivery, not
        a change in behaviour.

        Only two callers read this return value (ResendVerificationView and
        send_email_verification_code); every notify_* function below calls it
        and ignores the result, so a disabled mailer costs them nothing — the
        FCM push beside each one still goes.
        """
        if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
            logger.info('Email disabled; not sending to %s: %s', to_email, subject)
            return False

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
        """No SMS provider is wired up — and none ever delivered a message:
        the Twilio integration only ever ran with placeholder credentials,
        so every caller below has effectively been a no-op since day one.
        Push (FCM) is the transactional transport; phone verification is
        Firebase Phone Auth on the device. Kept as a stub so the call sites
        stay in place for whenever an Iraqi SMS gateway is contracted —
        implement it here, nowhere else."""
        logger.debug("SMS transport not configured; not sending to %s", to_phone)
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


def notify_push(user, title, body, data=None, actionable=False):
    """Save notification to DB and send FCM push to user's device.

    [actionable] يجعل الإشعار يحمل أزراراً على شاشة القفل — ويُرسَل
    بيانات فقط لأن أزراره يرسمها التطبيق لا أندرويد (انظر
    send_push_notification). يُرفع علم `actionable` في data ليعرف
    التطبيق أنه مطالَب ببناء إشعار بأزرار لا إشعار عاديّ.
    """
    from .models import Notification
    notification_type = (data or {}).get('type', 'general')
    payload = dict(data or {})
    if actionable:
        payload['actionable'] = '1'

    Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        body=body,
        data=payload,
    )
    if FIREBASE_AVAILABLE and user.fcm_token:
        send_push_notification(
            user.fcm_token, title, body, payload, data_only=actionable)


# Specific notification functions for different events

def notify_reservation_created(reservation):
    """Notify seller about new reservation and confirm to buyer"""
    seller = reservation.item.owner
    buyer = reservation.buyer
    item_title = bidi_isolate(reservation.item.title)

    subject = f"حجز جديد: {item_title}"
    message = f"""
    مرحباً {seller.username}،

    قام {buyer.username} بحجز منتجك "{item_title}".

    تفاصيل الحجز:
    - المنتج: {item_title}
    - العربون: {reservation.deposit_amount} IQD
    - السعر الإجمالي: {reservation.total_price} IQD
    - ينتهي في: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    يرجى تجهيز المنتج للمشتري.

    فريق سوقي
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(
        seller, subject, f'{bidi_isolate(buyer.username)} حجز منتجك "{item_title}".',
        data={'type': 'reservation_created', 'reservation_id': str(reservation.id)})

    # لا إشعار فوري للمشتري: هو الذي ضغط «احجز» قبل ثانية ورأى شاشة
    # التأكيد، فإشعار يخبره بما فعله للتوّ لا يضيف شيئاً. وأسوأ من ذلك
    # أن نصّه «تم تأكيد حجزك» كان يوحي بأن المعاملة انتهت، بينما الحجز
    # ما زال 'pending' وينتظر الدفع — فيكتشف بعضهم أنه انتهى صلاحيته.
    #
    # بدلاً منه: تذكير بعد ساعة يقول «أكمل حجزك»، ولا يُرسَل إن أكمله.
    # انظر remind_to_complete_reservation في core/tasks.py.
    if buyer.email:
        buyer_subject = f"أكمل حجزك: {item_title}"
        NotificationService.send_email(buyer.email, buyer_subject, f"""
    مرحباً {buyer.get_full_name() or buyer.username}،

    حجزت "{item_title}" — ويبقى إكمال الحجز قبل انتهاء المهلة.

    تفاصيل الحجز:
    - العربون: {reservation.deposit_amount} IQD
    - السعر الإجمالي: {reservation.total_price} IQD
    - ينتهي في: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    فريق الشورجة
    """)


def notify_complete_your_reservation(reservation):
    """تذكير المشتري بإكمال حجز ما زال معلّقاً.

    يُرسل بعد ساعة من الحجز (core/tasks.py)، وبعد التحقّق من أنه ما زال
    'pending' — فمن أكمل حجزه لا يُطالَب بإكماله.

    النوع 'reservation_expiring' لا نوع جديد: التطبيق يوجّهه إلى شاشة
    الحجوزات أصلاً، وهو ما نريده هنا بالضبط. المعنى واحد — حجز يحتاج
    فعلاً قبل أن تنتهي مهلته.
    """
    item_title = bidi_isolate(reservation.item.title)
    notify_push(
        reservation.buyer,
        'أكمل حجزك',
        f'أكمل حجزك لمنتج "{item_title}" قبل انتهاء المهلة.',
        data={'type': 'reservation_expiring',
              'reservation_id': str(reservation.id)},
        # تفاعليّ: زرّ «أكمل الحجز» يفتح الحجز مباشرة من شاشة القفل.
        # هذا تذكير لا خبر عاجل، فتأخّر رسائل البيانات مقبول هنا.
        actionable=True,
    )


def notify_reservation_cancelled_by_seller(reservation):
    """Notify buyer that the seller cancelled their reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer
    item_title = bidi_isolate(reservation.item.title)

    subject = f"تم إلغاء حجزك: {item_title}"
    message = f"""
    مرحباً {buyer.username}،

    قام البائع {seller.username} بإلغاء حجزك لمنتج "{item_title}".

    تفاصيل الحجز:
    - المنتج: {item_title}
    - السعر: {reservation.total_price} IQD

    سيتم استرداد المبلغ المدفوع إن وجد.

    نعتذر عن الإزعاج.
    فريق سوقي
    """

    NotificationService.send_email(buyer.email, subject, message)
    notify_push(
        buyer,
        subject,
        f'قام البائع بإلغاء حجزك لمنتج "{item_title}".',
        data={'type': 'reservation_cancelled_by_seller', 'reservation_id': str(reservation.id)}
    )

    if buyer.phone_verified and buyer.phone:
        sms_message = f"تم إلغاء حجزك لمنتج '{item_title}' من قبل البائع."
        NotificationService.send_sms(buyer.phone, sms_message)


def notify_reservation_cancelled_by_buyer(reservation):
    """Notify seller that the buyer cancelled their reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer
    item_title = bidi_isolate(reservation.item.title)

    subject = f"إلغاء حجز لمنتجك: {item_title}"
    message = f"""
    مرحباً {seller.username}،

    قام المشتري {buyer.username} بإلغاء حجزه لمنتجك "{item_title}".

    تفاصيل الحجز:
    - المنتج: {item_title}
    - السعر: {reservation.total_price} IQD

    أصبح منتجك متاحاً للمشترين الآخرين.

    فريق سوقي
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(
        seller,
        subject,
        f'ألغى {bidi_isolate(buyer.username)} حجزه لمنتجك "{item_title}".',
        data={'type': 'reservation_cancelled_by_buyer', 'reservation_id': str(reservation.id)}
    )


def notify_reservation_expiring(reservation, hours_remaining):
    """Notify buyer that reservation is expiring soon"""
    buyer = reservation.buyer
    item_title = bidi_isolate(reservation.item.title)

    subject = f"حجزك على وشك الانتهاء: {item_title}"
    message = f"""
    مرحباً {buyer.username}،

    حجزك لمنتج "{item_title}" سينتهي خلال {hours_remaining} ساعة!

    يرجى إتمام عملية الشراء لتجنب خسارة العربون البالغ {reservation.deposit_amount} IQD.

    المبلغ المتبقي: {reservation.total_price - reservation.deposit_amount} IQD
    ينتهي في: {reservation.expires_at.strftime('%Y-%m-%d %H:%M')}

    أكمل الشراء من هنا: {settings.FRONTEND_URL}/reservations/{reservation.id}

    فريق سوقي
    """

    NotificationService.send_email(buyer.email, subject, message)
    notify_push(buyer, subject, f"حجزك ينتهي خلال {hours_remaining} ساعة. أكمل عملية الشراء الآن!",
                data={'type': 'reservation_expiring', 'reservation_id': str(reservation.id)})

    if buyer.phone_verified and buyer.phone:
        sms_message = f"تذكير: حجزك لمنتج '{item_title}' ينتهي خلال {hours_remaining} ساعة. أكمل عملية الشراء الآن!"
        NotificationService.send_sms(buyer.phone, sms_message)


def notify_reservation_completed(reservation):
    """Notify seller and buyer when buyer completes a reservation"""
    seller = reservation.item.owner
    buyer = reservation.buyer
    item_title = bidi_isolate(reservation.item.title)

    # --- Notify seller ---
    seller_subject = f"تم شراء منتجك: {item_title}"
    seller_message = f"""
    مرحباً {seller.username}،

    تم اكمال الحجز وشراء منتجك "{item_title}" بواسطة {buyer.username}.

    تفاصيل البيع:
    - السعر النهائي: {reservation.total_price} IQD
    - المشتري: {buyer.username}
    - وقت الاكمال: {reservation.completed_at.strftime('%Y-%m-%d %H:%M')}

    يرجى التواصل مع المشتري لترتيب الاستلام.

    فريق سوقي
    """
    NotificationService.send_email(seller.email, seller_subject, seller_message)
    notify_push(
        seller,
        seller_subject,
        f'أكمل {bidi_isolate(buyer.username)} شراء منتجك "{item_title}" بمبلغ {reservation.total_price} IQD.',
        data={'type': 'reservation_completed', 'reservation_id': str(reservation.id)},
    )

    # --- Notify buyer ---
    buyer_subject = f"تم تأكيد شرائك: {item_title}"
    buyer_message = f"""
    مرحباً {buyer.username}،

    تم تأكيد عملية الشراء بنجاح لمنتج "{item_title}".

    تفاصيل الشراء:
    - المبلغ المدفوع: {reservation.total_price} IQD
    - البائع: {seller.username}
    - وقت الاكمال: {reservation.completed_at.strftime('%Y-%m-%d %H:%M')}

    يرجى التواصل مع البائع لترتيب الاستلام.

    فريق سوقي
    """
    NotificationService.send_email(buyer.email, buyer_subject, buyer_message)
    notify_push(
        buyer,
        buyer_subject,
        f'تم تأكيد شرائك لـ "{item_title}" بمبلغ {reservation.total_price} IQD.',
        data={'type': 'reservation_completed', 'reservation_id': str(reservation.id)},
    )


def notify_new_bid(auction, bid):
    """Notify about new bid on auction"""
    seller = auction.item.owner
    item_title = bidi_isolate(auction.item.title)

    subject = f"مزايدة جديدة: {item_title}"
    message = f"""
    مرحباً {seller.username}،

    تم تقديم مزايدة جديدة على مزادك "{item_title}".

    تفاصيل المزايدة:
    - المزايد: {bid.bidder.username}
    - المبلغ: {bid.amount} IQD
    - السعر السابق: {auction.current_price} IQD

    شاهد المزاد: {settings.FRONTEND_URL}/auctions/{auction.id}

    فريق سوقي
    """

    NotificationService.send_email(seller.email, subject, message)
    notify_push(seller, subject, f'{bidi_isolate(bid.bidder.username)} زايد بمبلغ {bid.amount} IQD على "{item_title}"',
                data={'type': 'new_bid', 'auction_id': str(auction.id), 'bid_amount': str(bid.amount)})

    # Notify previous highest bidder they've been outbid
    previous_winning_bids = auction.bids.filter(
        is_winning=True
    ).exclude(id=bid.id)

    for prev_bid in previous_winning_bids:
        outbid_subject = f"تم تجاوز مزايدتك: {item_title}"
        outbid_message = f"""
        مرحباً {prev_bid.bidder.username}،

        تم تجاوز مزايدتك على "{item_title}".

        مزايدتك: {prev_bid.amount} IQD
        أعلى مزايدة حالياً: {bid.amount} IQD

        قدّم مزايدة أعلى: {settings.FRONTEND_URL}/auctions/{auction.id}

        فريق سوقي
        """
        NotificationService.send_email(prev_bid.bidder.email, outbid_subject, outbid_message)
        notify_push(prev_bid.bidder, outbid_subject, f'تم تجاوز مزايدتك على "{item_title}". الأعلى الآن: {bid.amount} IQD',
                    data={'type': 'outbid', 'auction_id': str(auction.id)})


def notify_auction_ending_soon(auction, minutes_remaining):
    """Notify bidders that auction is ending soon"""
    bidders = auction.bids.values_list('bidder', flat=True).distinct()

    from django.contrib.auth import get_user_model
    User = get_user_model()
    item_title = bidi_isolate(auction.item.title)

    for user_id in bidders:
        try:
            user = User.objects.get(id=user_id)

            subject = f"المزاد ينتهي قريباً: {item_title}"
            message = f"""
            مرحباً {user.username}،

            سينتهي المزاد على "{item_title}" خلال {minutes_remaining} دقيقة!

            السعر الحالي: {auction.current_price} IQD
            أقل زيادة مسموحة: {auction.minimum_increment} IQD

            قدّم مزايدتك الأخيرة: {settings.FRONTEND_URL}/auctions/{auction.id}

            فريق سوقي
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
    item_title = bidi_isolate(auction.item.title)

    subject = f"مبروك! فزت بمزاد: {item_title}"
    message = f"""
    مرحباً {winner.username}،

    مبروك! فزت بمزاد "{item_title}" بمبلغ {reservation.total_price} IQD.

    لقد دفعت عربون مزايدة قدره {reservation.deposit_amount} IQD. اختر طريقة دفع الباقي:
    1. ادفع المتبقي {remaining} IQD الآن، ثم رتّب التوصيل
    2. أبقِ عربونك كما هو وادفع المتبقي {remaining} IQD نقداً عند التوصيل

    يرجى الاختيار خلال 48 ساعة — وإلا سنعتمد الخيار الثاني تلقائياً.

    أكمل حجزك: {settings.FRONTEND_URL}/reservations/{reservation.id}

    فريق سوقي
    """

    NotificationService.send_email(winner.email, subject, message)
    notify_push(
        winner, subject,
        f'فزت بمزاد "{item_title}" بمبلغ {reservation.total_price} IQD! أكمل حجزك لاختيار طريقة الدفع.',
        data={
            'type': 'auction_won',
            'auction_id': str(auction.id),
            'reservation_id': str(reservation.id),
        }
    )

    if winner.phone_verified and winner.phone:
        sms_message = f"مبروك! فزت بمزاد '{item_title}' بمبلغ {reservation.total_price} IQD. أكمل حجزك الآن!"
        NotificationService.send_sms(winner.phone, sms_message)

    # Notify seller
    seller = auction.item.owner
    seller_subject = f"انتهى مزادك: {item_title}"
    seller_message = f"""
    مرحباً {seller.username}،

    انتهى مزادك على "{item_title}"!

    الفائز: {winner.username}
    السعر النهائي: {reservation.total_price} IQD

    سيكمل المشتري حجزه قريباً (إما بدفع كامل المبلغ الآن، أو الباقي نقداً عند
    التوصيل). يرجى تجهيز المنتج للتوصيل.

    فريق سوقي
    """

    NotificationService.send_email(seller.email, seller_subject, seller_message)


def notify_auction_no_winner(auction):
    """Notify seller that auction ended with no winner"""
    seller = auction.item.owner
    item_title = bidi_isolate(auction.item.title)

    subject = f"انتهى مزادك: {item_title}"
    message = f"""
    مرحباً {seller.username}،

    انتهى مزادك على "{item_title}" دون أي مزايدات فائزة.

    يمكنك:
    1. إنشاء مزاد جديد بسعر بدء أقل
    2. عرضه كمنتج بسعر ثابت
    3. الاحتفاظ به في متجرك

    إدارة منتجك: {settings.FRONTEND_URL}/items/{auction.item.id}

    فريق سوقي
    """

    NotificationService.send_email(seller.email, subject, message)


def notify_representative_assigned(representative):
    """Notify representative about assignment"""
    item_title = bidi_isolate(representative.item.title)

    subject = f"طلب تمثيل: {item_title}"
    message = f"""
    مرحباً {representative.representative.username}،

    يريدك {representative.owner.username} أن تكون ممثلاً لبيع "{item_title}".

    العمولة: {representative.commission_percentage}%

    الصلاحيات:
    - يمكنه الإدراج: {'نعم' if representative.can_list else 'لا'}
    - يمكنه التفاوض: {'نعم' if representative.can_negotiate else 'لا'}
    - يمكنه إتمام البيع: {'نعم' if representative.can_complete_sale else 'لا'}

    اقبل أو ارفض الطلب: {settings.FRONTEND_URL}/representatives/{representative.id}

    فريق سوقي
    """

    NotificationService.send_email(representative.representative.email, subject, message)


def notify_new_message(message):
    """Notify user about new chat message"""
    recipient = message.chat.seller if message.sender == message.chat.buyer else message.chat.buyer

    # A chat may be about an item, or a direct conversation with a store.
    chat = message.chat
    about_raw = chat.item.title if chat.item else (chat.store.name if chat.store else 'متجرك')
    about = bidi_isolate(about_raw)
    sender_name = bidi_isolate(message.sender.username)

    subject = f"رسالة جديدة: {about}"
    email_message = f"""
    مرحباً {recipient.username}،

    لديك رسالة جديدة من {message.sender.username} بخصوص "{about}".

    الرسالة: {message.content[:100]}{'...' if len(message.content) > 100 else ''}

    عرض المحادثة: {settings.FRONTEND_URL}/chats/{message.chat.id}

    فريق سوقي
    """

    # الدفع (FCM) أولاً: هذه الدالة تُستدعى من داخل ChatConsumer.receive أثناء
    # جلسة محادثة حيّة، والبريد الإلكتروني (SMTP) قد يستغرق ثوانٍ — استدعاؤه
    # قبل الدفع كان يؤخّر وصول الإشعار الفوري على الجوال بلا داعٍ، رغم أن لا
    # أحد ينتظر البريد بنفس السرعة.
    #
    # الحقول الإضافية (content/message_type/created_at) تتيح لتطبيق الجوال
    # (ChatsProvider.handleIncomingMessagePush) تحديث صف المحادثة في القائمة
    # محلياً فوراً من حمولة FCM نفسها، بدل طلب REST كامل لإعادة تحميل القائمة
    # بعد وصول كل إشعار — يحذف جولة شبكة كاملة من زمن الوصول المُدرَك.
    notify_push(recipient, subject, f"رسالة جديدة من {sender_name}: {message.content[:80]}",
                data={
                    'type': 'new_message',
                    'chat_id': str(message.chat.id),
                    'content': message.content,
                    'message_type': message.message_type,
                    'created_at': message.created_at.isoformat(),
                })
    NotificationService.send_email(recipient.email, subject, email_message)


def notify_dispute_created(dispute):
    """Notify admin and defendant about new dispute"""
    dispute_title = bidi_isolate(dispute.title)
    item_title = bidi_isolate(dispute.item.title)

    # Notify defendant
    subject = f"تم فتح نزاع: {dispute_title}"
    message = f"""
    مرحباً {dispute.defendant.username}،

    قام {dispute.complainant.username} بفتح نزاع ضدك.

    المنتج: {item_title}
    المشكلة: {dispute.description}

    يرجى الرد على النزاع: {settings.FRONTEND_URL}/disputes/{dispute.id}

    فريق سوقي
    """

    NotificationService.send_email(dispute.defendant.email, subject, message)

    # Notify admin (you can have a list of admin emails)
    admin_subject = f"نزاع جديد: {dispute_title}"
    admin_message = f"""
    تم فتح نزاع جديد:

    المُشتكي: {dispute.complainant.username}
    المُشتكى عليه: {dispute.defendant.username}
    المنتج: {item_title}

    مراجعة النزاع: {settings.FRONTEND_URL}/disputes/{dispute.id}
    """

    # Send to all admin emails, if any are configured
    for admin_email in getattr(settings, 'ADMIN_EMAILS', []):
        NotificationService.send_email(admin_email, admin_subject, admin_message)


_REPORT_TARGET_LABELS_AR = {'item': 'منتج', 'store': 'متجر', 'user': 'مستخدم', 'reel': 'ريل'}


def notify_report_submitted(report):
    """Notify admins that a new policy-violation report needs review"""
    target_label = _REPORT_TARGET_LABELS_AR[report.target_type]
    target_name = bidi_isolate(str(report.target))

    admin_subject = f"بلاغ جديد عن {target_label}: {target_name}"
    admin_message = f"""
    تم استلام بلاغ جديد:

    المُبلِغ: {report.reporter.username}
    نوع الهدف: {target_label}
    الهدف: {target_name}
    السبب: {report.get_reason_display()}
    التفاصيل: {report.description or '—'}

    مراجعة البلاغ: {settings.FRONTEND_URL}/admin/reports/{report.id}
    """

    for admin_email in getattr(settings, 'ADMIN_EMAILS', []):
        NotificationService.send_email(admin_email, admin_subject, admin_message)


def notify_report_reviewed(report):
    """Notify the reporter that their report was reviewed"""
    if report.status == 'resolved':
        title = "تم اتخاذ إجراء بخصوص بلاغك"
        body = "راجع فريقنا بلاغك واتخذ الإجراء المناسب. شكراً لمساعدتك في الحفاظ على أمان المنصة."
    else:
        title = "تمت مراجعة بلاغك"
        body = "راجع فريقنا بلاغك ولم يجد ما يستدعي اتخاذ إجراء."

    notify_push(
        report.reporter, title, body,
        data={'type': 'report_reviewed', 'report_id': str(report.id), 'status': report.status}
    )


def notify_delivery_assigned(delivery_order):
    """Notify the delivery company's account that a new order was handed to them"""
    company = delivery_order.company
    if not company or not company.account:
        return

    account = company.account
    subject = f"طلب توصيل جديد #{delivery_order.id}"
    body = (
        f'استلام من: {bidi_isolate(delivery_order.from_name)} ({delivery_order.from_phone})\n'
        f'تسليم إلى: {bidi_isolate(delivery_order.to_name)} ({delivery_order.to_phone})\n'
        f'المنتج: {bidi_isolate(delivery_order.item_summary)}'
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
    body = f'وصل "{bidi_isolate(delivery_order.item_summary)}". اضغط "تم الاستلام" إذا استلمته بحالة جيدة، أو "رفض الاستلام" إذا كانت هناك مشكلة.'

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
        f'رفض {bidi_isolate(reservation.buyer.username)} استلام "{bidi_isolate(delivery_order.item_summary)}". سيُعاد المنتج إليك عبر شركة التوصيل.',
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
        f'ستتواصل شركة التوصيل معك لاستلام "{bidi_isolate(delivery_order.item_summary)}" منك. سيُعاد مبلغك بعد استلامهم للمنتج.',
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
    body = f'استلمت شركة التوصيل "{bidi_isolate(reservation.item.title)}" منك، وتم إعادة {amount} إلى محفظتك.'

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
        f'حصّلت شركة التوصيل {delivery_order.cash_to_collect} من {bidi_isolate(reservation.buyer.username)} '
        f'مقابل "{bidi_isolate(reservation.item.title)}" — أصبح المبلغ كاملاً قابلاً للتحرير لك.'
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
    subject = "تمت عملية الدفع بنجاح"
    item_title = bidi_isolate(transaction.item.title) if transaction.item else 'لا يوجد'
    message = f"""
    مرحباً {transaction.user.username}،

    تمت معالجة دفعتك بمبلغ {transaction.amount} IQD بنجاح.

    نوع العملية: {_transaction_type_label(transaction)}
    المنتج: {item_title}

    عرض العملية: {settings.FRONTEND_URL}/transactions/{transaction.id}

    فريق سوقي
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_payment_failed(transaction):
    """Notify user about failed payment"""
    subject = "فشلت عملية الدفع"
    message = f"""
    مرحباً {transaction.user.username}،

    فشلت عملية دفعك بمبلغ {transaction.amount} IQD.

    يرجى تحديث طريقة الدفع والمحاولة مرة أخرى.

    أعد المحاولة: {settings.FRONTEND_URL}/transactions/{transaction.id}/retry

    فريق سوقي
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_refund_processed(transaction):
    """Notify user about refund"""
    subject = "تم استرداد مبلغك"
    item_title = bidi_isolate(transaction.item.title) if transaction.item else 'لا يوجد'
    message = f"""
    مرحباً {transaction.user.username}،

    تم استرداد مبلغ {transaction.amount} IQD إلى حسابك.

    نوع العملية: {_transaction_type_label(transaction)}
    المنتج: {item_title}

    سيظهر المبلغ المسترد في حسابك خلال 5-10 أيام عمل.

    فريق سوقي
    """

    NotificationService.send_email(transaction.user.email, subject, message)


def notify_live_stream_started(session):
    """Notify a store's followers that it has just gone live (push only — real-time, no email)"""
    from .models import StoreFollow

    store_name = bidi_isolate(session.store.name)
    session_title = bidi_isolate(session.title)

    follows = StoreFollow.objects.filter(store=session.store).select_related('follower')
    for follow in follows:
        notify_push(
            follow.follower,
            f'{store_name} يبث الآن مباشرة!',
            f'"{session_title}" بدأ الآن — انضم قبل فوات الأوان.',
            data={
                'type': 'live_stream_started',
                'live_session_id': str(session.id),
                'store_id': str(session.store_id),
            }
        )


def notify_reel_posted(reel):
    """Notify a store's followers that it just posted a new reel (push only,
    same fan-out shape as notify_live_stream_started)."""
    from .models import StoreFollow

    store_name = bidi_isolate(reel.store.name)

    follows = StoreFollow.objects.filter(store=reel.store).select_related('follower')
    for follow in follows:
        notify_push(
            follow.follower,
            f'{store_name} نشر ريلًا جديدًا!',
            'شاهد أحدث فيديو من متجرك المفضل الآن.',
            data={
                'type': 'reel_posted',
                'reel_id': str(reel.id),
                'store_id': str(reel.store_id),
            }
        )


def notify_live_item_reserved(reservation):
    """Notify the item's owner that a viewer reserved it during a live session"""
    owner = reservation.item.owner
    notify_push(
        owner,
        'تم حجز منتجك أثناء البث المباشر',
        f'{bidi_isolate(reservation.buyer.username)} حجز "{bidi_isolate(reservation.item.title)}" بعربون {reservation.deposit_amount}.',
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
        f'نفدت كمية "{bidi_isolate(queue_item.item.title)}" أثناء البث المباشر.',
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


# ── الريلز ───────────────────────────────────────────────────────────────

# عتبات الإعجاب. إشعار لكل إعجاب يعني مئتي إشعار على ريل ناجح واحد —
# فيُطفئ التاجر إشعارات التطبيق كلها، ويخسر معها إشعارات الحجوزات
# والمزادات. العتبات تجعل الإشعار خبراً ("وصل ريلك إلى 100 إعجاب")
# لا ضجيجاً، وتُشتقّ من likes_count بلا حالة إضافية تُخزَّن.
REEL_LIKE_MILESTONES = (1, 10, 50, 100, 500, 1000, 5000)


def notify_reel_liked(reel, liker):
    """يُشعر صاحب الريل عند أول إعجاب ثم عند كل عتبة."""
    owner = reel.store.user if reel.store_id else None
    # لا إشعار لصاحبه عن نفسه: إعجاب المرء بريله لا خبر فيه.
    if owner is None or owner.pk == liker.pk:
        return
    if reel.likes_count not in REEL_LIKE_MILESTONES:
        return

    if reel.likes_count == 1:
        body = f'أعجب {bidi_isolate(liker.get_full_name() or liker.username)} بريلك'
    else:
        body = f'وصل ريلك إلى {reel.likes_count} إعجاب'

    notify_push(
        owner, 'إعجاب جديد', body,
        data={'type': 'reel_liked', 'reel_id': str(reel.id)},
    )


def notify_reel_comment(comment):
    """يُشعر صاحب الريل بكل تعليق.

    بلا عتبات هنا: التعليق كلام موجَّه وغالباً سؤال عن البضاعة، وتأخيره
    يُضيّع بيعاً. وحجمه أقل من الإعجاب بمراتب فلا يُغرق.
    """
    reel = comment.reel
    owner = reel.store.user if reel.store_id else None
    if owner is None or owner.pk == comment.user_id:
        return

    author = bidi_isolate(comment.user.get_full_name() or comment.user.username)
    preview = comment.content.strip()
    # الإشعار يُعرض في سطرٍ على القفل؛ النصّ الطويل يُقطع بلا إشارة.
    if len(preview) > 60:
        preview = preview[:60].rstrip() + '…'

    notify_push(
        owner, 'تعليق جديد على ريلك', f'{author}: {preview}',
        data={
            'type': 'reel_comment',
            'reel_id': str(reel.id),
            'comment_id': str(comment.id),
        },
    )
