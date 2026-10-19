import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.db.models import F
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()

# "حضور" الطرفين في نفس المحادثة — يُستخدم لتفادي دفع (FCM) لا داعي له حين
# يكون المستلم أصلاً بداخل نفس المحادثة ويرى الرسالة حيّاً عبر الـ WebSocket.
# مبني على Redis (نفس الـ cache المُستخدم في المشروع أصلاً) بدل عدّاد ذاكرة
# محلي حتى يعمل بشكل صحيح مهما كان عدد عمليات الخادم (workers). عَلَم بسيط
# (وليس عدّاد اتصالات) لتفادي أي تعقيد إضافي — الحد الأقصى (TTL) هو شبكة أمان
# فقط لانقطاع غير سليم (مثل إغلاق التطبيق فجأة)؛ الاتصال/الانقطاع الطبيعيين
# يضبطانه فوراً.
_CHAT_PRESENCE_TTL = 600


def _chat_presence_key(chat_id, user_id):
    return f'chat_presence:{chat_id}:{user_id}'


def mark_chat_present(chat_id, user_id):
    from django.core.cache import cache
    cache.set(_chat_presence_key(chat_id, user_id), True, _CHAT_PRESENCE_TTL)


def mark_chat_absent(chat_id, user_id):
    from django.core.cache import cache
    cache.delete(_chat_presence_key(chat_id, user_id))


def is_user_present_in_chat(chat_id, user_id):
    """True إن كانت لدى `user_id` هذه اللحظة اتصال WebSocket مفتوح بهذه
    المحادثة تحديداً — تُستخدم لتخطّي إشعار دفع مكرر لرسالة يراها أصلاً حيّاً،
    وكإشارة "متصل الآن" في واجهة المحادثة (ChatDetailSerializer.other_user_online)."""
    from django.core.cache import cache
    return bool(cache.get(_chat_presence_key(chat_id, user_id)))


def _touch_last_seen(user_id):
    """Cheap last-activity timestamp — updated whenever a user connects to
    or disconnects from a chat (see ChatConsumer below), without needing a
    dedicated global presence heartbeat."""
    User.objects.filter(id=user_id).update(last_seen_at=timezone.now())


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time chat
    """

    async def connect(self):
        """Handle WebSocket connection"""
        self.chat_id = self.scope['url_route']['kwargs']['chat_id']
        self.room_group_name = f'chat_{self.chat_id}'
        self.user = self.scope['user']

        # Check if user is authenticated
        if not self.user.is_authenticated:
            await self.close()
            return

        # Check if user is participant of this chat
        is_participant = await self.check_participant()
        if not is_participant:
            await self.close()
            return

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        await database_sync_to_async(mark_chat_present)(self.chat_id, self.user.id)
        await database_sync_to_async(_touch_last_seen)(self.user.id)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'presence_update', 'user_id': self.user.id, 'is_online': True}
        )

        # Mark messages as read, and let the other participant's open
        # thread know live (so a sent message's tick can flip to "seen"
        # without them needing to refresh).
        await self._mark_and_broadcast_read()

        logger.info(f"User {self.user.username} connected to chat {self.chat_id}")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
        if getattr(self, 'user', None) and self.user.is_authenticated:
            await database_sync_to_async(mark_chat_absent)(self.chat_id, self.user.id)
            await database_sync_to_async(_touch_last_seen)(self.user.id)
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'presence_update', 'user_id': self.user.id, 'is_online': False}
            )

        logger.info(f"User {self.user.username} disconnected from chat {self.chat_id}")

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type', 'message')

            if message_type == 'message':
                await self.handle_chat_message(data)
            elif message_type == 'typing':
                await self.handle_typing(data)
            elif message_type == 'read':
                await self._mark_and_broadcast_read()

        except json.JSONDecodeError:
            logger.error("Invalid JSON received")
        except Exception as e:
            logger.error(f"Error in receive: {str(e)}")

    async def handle_chat_message(self, data):
        """Handle incoming chat message"""
        message_content = data.get('message', '').strip()

        if not message_content:
            return

        # Save message to database
        message = await self.save_message(message_content)

        # Send message to room group
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': message_content,
                'sender': self.user.username,
                'sender_id': self.user.id,
                'message_id': message['id'],
                'timestamp': message['timestamp'],
                'message_type': 'user',
                'attachment_url': None,
            }
        )

        # لا داعي لإشعار (دفع + بريد) إن كان الطرف الآخر بنفس المحادثة هذه
        # اللحظة — سيراها فوراً عبر البثّ أعلاه، فالإشعار هنا سيكون تكراراً
        # مزعجاً لا فائدة منه.
        recipient_id = await self.get_recipient_id()
        if recipient_id is None or not await database_sync_to_async(
                is_user_present_in_chat)(self.chat_id, recipient_id):
            await self.send_message_notification(message)

    async def handle_typing(self, data):
        """Handle typing indicator"""
        is_typing = data.get('is_typing', False)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'typing_indicator',
                'user': self.user.username,
                'user_id': self.user.id,
                'is_typing': is_typing
            }
        )

    async def chat_message(self, event):
        """Send chat message to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'message',
            'message': event['message'],
            'sender': event['sender'],
            'sender_id': event['sender_id'],
            'message_id': event['message_id'],
            'timestamp': event['timestamp'],
            'message_type': event.get('message_type', 'user'),
            'attachment_url': event.get('attachment_url'),
            'offer_amount': event.get('offer_amount'),
            'offer_quantity': event.get('offer_quantity'),
            'offer_status': event.get('offer_status'),
        }))

    async def typing_indicator(self, event):
        """Send typing indicator to WebSocket"""
        # Don't send typing indicator to the sender
        if event['user_id'] != self.user.id:
            await self.send(text_data=json.dumps({
                'type': 'typing',
                'user': event['user'],
                'is_typing': event['is_typing']
            }))

    async def presence_update(self, event):
        """Relay the other participant's online/offline flip live — don't
        echo back to the sender's own connection."""
        if event['user_id'] != self.user.id:
            await self.send(text_data=json.dumps({
                'type': 'presence',
                'user_id': event['user_id'],
                'is_online': event['is_online'],
            }))

    async def messages_read(self, event):
        """Tell the group someone just read up to now, so the sender's open
        thread can flip their own sent bubbles' tick to "seen" live."""
        await self.send(text_data=json.dumps({
            'type': 'messages_read',
            'reader_id': event['reader_id'],
        }))

    async def _mark_and_broadcast_read(self):
        marked_count = await self.mark_messages_read()
        if marked_count:
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'messages_read', 'reader_id': self.user.id}
            )

    @database_sync_to_async
    def check_participant(self):
        """Check if user is a participant of the chat"""
        from .models import Chat
        try:
            chat = Chat.objects.get(id=self.chat_id)
            return chat.buyer == self.user or chat.seller == self.user
        except Chat.DoesNotExist:
            return False

    @database_sync_to_async
    def get_recipient_id(self):
        """The *other* participant's user id — used to check whether they're
        currently present in this same chat before bothering with a push."""
        from .models import Chat
        try:
            chat = Chat.objects.get(id=self.chat_id)
            return chat.get_other_user(self.user).id
        except Chat.DoesNotExist:
            return None

    @database_sync_to_async
    def save_message(self, content):
        """Save message to database"""
        from .models import Chat, Message

        chat = Chat.objects.get(id=self.chat_id)
        message = Message.objects.create(
            chat=chat,
            sender=self.user,
            content=content,
            message_type='user'
        )

        return {
            'id': message.id,
            'timestamp': message.created_at.isoformat()
        }

    @database_sync_to_async
    def mark_messages_read(self):
        """Mark all unread messages in chat as read for current user.
        Returns how many were just marked, so the caller only broadcasts a
        `messages_read` event when something actually changed."""
        from .models import Chat

        try:
            chat = Chat.objects.get(id=self.chat_id)
            return chat.messages.exclude(sender=self.user).filter(
                is_read=False
            ).update(is_read=True)
        except Chat.DoesNotExist:
            return 0

    @database_sync_to_async
    def send_message_notification(self, message_data):
        """Notify the other participant — push (FCM) + email."""
        from .models import Chat
        from .notifications import notify_new_message

        try:
            chat = Chat.objects.get(id=self.chat_id)
            latest_message = chat.messages.latest('created_at')
            # notify_new_message already sends both the FCM push (with the
            # richer chat-list payload) and the email — a second, separate
            # send_push_notification() call used to sit here too, silently
            # double-notifying the recipient for every plain-text message.
            notify_new_message(latest_message)
        except Exception as e:
            logger.error(f"Error sending notification: {str(e)}")


class NotificationConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time notifications
    """

    async def connect(self):
        """Handle WebSocket connection"""
        self.user = self.scope['user']

        if not self.user.is_authenticated:
            await self.close()
            return

        self.user_group_name = f'notifications_{self.user.id}'

        # Join user's notification group
        await self.channel_layer.group_add(
            self.user_group_name,
            self.channel_name
        )

        await self.accept()

        logger.info(f"User {self.user.username} connected to notifications")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        await self.channel_layer.group_discard(
            self.user_group_name,
            self.channel_name
        )

        logger.info(f"User {self.user.username} disconnected from notifications")

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            notification_type = data.get('type')

            if notification_type == 'mark_read':
                notification_id = data.get('notification_id')
                await self.mark_notification_read(notification_id)

        except json.JSONDecodeError:
            logger.error("Invalid JSON received")
        except Exception as e:
            logger.error(f"Error in receive: {str(e)}")

    async def notification(self, event):
        """Send notification to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'notification',
            'notification_type': event['notification_type'],
            'title': event['title'],
            'message': event['message'],
            'data': event.get('data', {}),
            'timestamp': event['timestamp']
        }))

    @database_sync_to_async
    def mark_notification_read(self, notification_id):
        """Mark notification as read"""
        # Implement notification read logic here
        pass


class AuctionConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time auction updates
    Users watching an auction page will receive live bid updates
    """

    async def connect(self):
        """Handle WebSocket connection"""
        self.auction_id = self.scope['url_route']['kwargs']['auction_id']
        self.room_group_name = f'auction_{self.auction_id}'
        self.user = self.scope['user']

        # Allow both authenticated and anonymous users to watch auctions
        # But only authenticated users can bid

        # Join auction room
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # Send current auction state
        await self.send_auction_state()

        logger.info(f"User {getattr(self.user, 'username', 'anonymous')} connected to auction {self.auction_id}")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

        logger.info(f"User disconnected from auction {self.auction_id}")

    async def receive(self, text_data):
        """Receive message from WebSocket"""
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                # Keep-alive ping
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': timezone.now().isoformat()
                }))

        except json.JSONDecodeError:
            logger.error("Invalid JSON received")
        except Exception as e:
            logger.error(f"Error in receive: {str(e)}")

    async def auction_update(self, event):
        """Send auction update to WebSocket"""
        await self.send(text_data=json.dumps({
            'type': 'auction_update',
            'event': event.get('event'),
            'auction_id': event.get('auction_id'),
            'data': event.get('data'),
            'timestamp': timezone.now().isoformat()
        }))

    @database_sync_to_async
    def send_auction_state(self):
        """Send current auction state when user connects"""
        from .models import AuctionListing
        from .serializers import AuctionListingSerializer

        try:
            auction = AuctionListing.objects.get(id=self.auction_id)
            serializer = AuctionListingSerializer(auction)

            # Send auction state asynchronously
            from asgiref.sync import async_to_sync
            async_to_sync(self.send)(text_data=json.dumps({
                'type': 'auction_state',
                'data': serializer.data
            }))
        except AuctionListing.DoesNotExist:
            pass


class LiveSessionConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for a live shopping broadcast's control/data channel.
    Video/audio itself is carried by the media server, not this socket —
    relays viewer count, "now showing" product changes, reservations, likes,
    and stream lifecycle events. Anonymous viewing is allowed (same as
    AuctionConsumer); all writes happen over REST, this is broadcast-only.
    """

    async def connect(self):
        """Handle WebSocket connection"""
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.room_group_name = f'live_{self.session_id}'
        self.user = self.scope['user']

        if await self.is_banned():
            await self.close(code=4403)
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        viewer_count = await self.increment_viewer_count()
        if viewer_count is not None:
            await self.track_viewer_joined()
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'live_event', 'event': 'viewer_count', 'data': {'current_viewer_count': viewer_count}}
            )

        logger.info(f"User {getattr(self.user, 'username', 'anonymous')} connected to live session {self.session_id}")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection"""
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

        viewer_count = await self.decrement_viewer_count()
        if viewer_count is not None:
            await self.track_viewer_left()
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'live_event', 'event': 'viewer_count', 'data': {'current_viewer_count': viewer_count}}
            )

        logger.info(f"User disconnected from live session {self.session_id}")

    async def receive(self, text_data):
        """Ping/pong keep-alive only — all mutations go through REST."""
        try:
            data = json.loads(text_data)
            if data.get('type') == 'ping':
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': timezone.now().isoformat()
                }))
        except json.JSONDecodeError:
            logger.error("Invalid JSON received")
        except Exception as e:
            logger.error(f"Error in receive: {str(e)}")

    async def live_event(self, event):
        """Relay a broadcast_live_event() call to this connected client"""
        await self.send(text_data=json.dumps({
            'type': event.get('event'),
            'data': event.get('data'),
            'timestamp': timezone.now().isoformat()
        }))

    @database_sync_to_async
    def is_banned(self):
        """Checked before accept() so a banned viewer can't (re)join this
        session's room at all. Only meaningful for signed-in users — see
        LiveSessionBan's docstring; the ban is scoped to this session only."""
        from .models import LiveSessionBan

        if not self.user or not self.user.is_authenticated:
            return False
        return LiveSessionBan.objects.filter(session_id=self.session_id, user_id=self.user.id).exists()

    @database_sync_to_async
    def increment_viewer_count(self):
        from .models import LiveSession
        from django.db.models.functions import Greatest

        updated = LiveSession.objects.filter(id=self.session_id).update(
            current_viewer_count=F('current_viewer_count') + 1,
            total_views=F('total_views') + 1,
        )
        if not updated:
            return None
        LiveSession.objects.filter(id=self.session_id).update(
            peak_viewer_count=Greatest(F('peak_viewer_count'), F('current_viewer_count'))
        )
        return LiveSession.objects.filter(id=self.session_id).values_list(
            'current_viewer_count', flat=True
        ).first()

    @database_sync_to_async
    def decrement_viewer_count(self):
        from .models import LiveSession

        updated = LiveSession.objects.filter(
            id=self.session_id, current_viewer_count__gt=0
        ).update(current_viewer_count=F('current_viewer_count') - 1)
        if not updated:
            return None
        return LiveSession.objects.filter(id=self.session_id).values_list(
            'current_viewer_count', flat=True
        ).first()

    @database_sync_to_async
    def track_viewer_joined(self):
        """Upsert this signed-in user's LiveSessionViewer row. Anonymous
        viewers have no identity to persist, so they're skipped here — they
        already counted toward current_viewer_count above."""
        from .models import LiveSessionViewer

        if not self.user or not self.user.is_authenticated:
            return

        viewer, created = LiveSessionViewer.objects.get_or_create(
            session_id=self.session_id, user=self.user,
            defaults={'active_connections': 1, 'is_watching': True, 'last_seen_at': timezone.now()}
        )
        if not created:
            LiveSessionViewer.objects.filter(pk=viewer.pk).update(
                active_connections=F('active_connections') + 1,
                is_watching=True,
                last_seen_at=timezone.now(),
            )

    @database_sync_to_async
    def track_viewer_left(self):
        """Mirror of track_viewer_joined — only flips is_watching off once
        every open connection from this user (all tabs/devices) has closed."""
        from .models import LiveSessionViewer

        if not self.user or not self.user.is_authenticated:
            return

        LiveSessionViewer.objects.filter(
            session_id=self.session_id, user=self.user, active_connections__gt=0
        ).update(active_connections=F('active_connections') - 1, last_seen_at=timezone.now())
        LiveSessionViewer.objects.filter(
            session_id=self.session_id, user=self.user, active_connections=0
        ).update(is_watching=False)

# ============================================
# Utility function to send notifications
# ============================================

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from datetime import datetime


def send_websocket_notification(user_id, notification_type, title, message, data=None):
    """
    Send notification via WebSocket to a specific user
    """
    channel_layer = get_channel_layer()

    try:
        async_to_sync(channel_layer.group_send)(
            f'notifications_{user_id}',
            {
                'type': 'notification',
                'notification_type': notification_type,
                'title': title,
                'message': message,
                'data': data or {},
                'timestamp': datetime.now().isoformat()
            }
        )
        logger.info(f"Sent notification to user {user_id}")
    except Exception as e:
        logger.error(f"Error sending WebSocket notification: {str(e)}")


def broadcast_chat_message(message, attachment_url=None):
    """
    Push a REST-created Message (e.g. an image upload) into the same
    Channels group the WebSocket path already broadcasts to, so it
    reaches any connected participant live.
    """
    channel_layer = get_channel_layer()

    try:
        async_to_sync(channel_layer.group_send)(
            f'chat_{message.chat_id}',
            {
                'type': 'chat_message',
                'message': message.content,
                'sender': message.sender.username if message.sender else 'System',
                'sender_id': message.sender_id,
                'message_id': message.id,
                'timestamp': message.created_at.isoformat(),
                'message_type': message.message_type,
                'attachment_url': attachment_url,
                'offer_amount': str(message.offer_amount) if message.offer_amount is not None else None,
                'offer_quantity': message.offer_quantity,
                'offer_status': message.offer_status,
            }
        )
    except Exception as e:
        logger.error(f"Error broadcasting chat message: {str(e)}")


def broadcast_live_event(session_id, event, data=None):
    """
    Fan out a live-shopping event (product_now_showing, item_reserved,
    item_sold_out, like_update, stream_started, stream_ended, ...) to
    everyone currently connected to this live session's WebSocket room.
    """
    channel_layer = get_channel_layer()

    try:
        async_to_sync(channel_layer.group_send)(
            f'live_{session_id}',
            {'type': 'live_event', 'event': event, 'data': data or {}}
        )
    except Exception as e:
        logger.error(f"Error broadcasting live event: {str(e)}")


# ============================================
# Example usage in your views/tasks
# ============================================

"""
# In your views or Celery tasks:

from core.routing import send_websocket_notification

# When a new bid is placed
send_websocket_notification(
    user_id=seller.id,
    notification_type='new_bid',
    title='New Bid on Your Auction',
    message=f'{bidder.username} placed a bid of ${bid.amount}',
    data={
        'auction_id': auction.id,
        'bid_amount': str(bid.amount),
        'bidder': bidder.username
    }
)

# When reservation is expiring
send_websocket_notification(
    user_id=buyer.id,
    notification_type='reservation_expiring',
    title='Reservation Expiring Soon',
    message=f'Your reservation expires in {hours} hours',
    data={
        'reservation_id': reservation.id,
        'hours_remaining': hours
    }
)

# When auction is won
send_websocket_notification(
    user_id=winner.id,
    notification_type='auction_won',
    title='Congratulations!',
    message=f'You won the auction for {item.title}',
    data={
        'auction_id': auction.id,
        'item_title': item.title,
        'winning_bid': str(auction.current_price)
    }
)
"""