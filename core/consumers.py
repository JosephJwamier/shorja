import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.db.models import F
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()


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

        # Send notification to other participant
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
        """Send WebSocket + FCM push notification to other participant"""
        from .models import Chat
        from .notifications import notify_new_message
        from .firebase import send_push_notification, FIREBASE_AVAILABLE

        try:
            chat = Chat.objects.get(id=self.chat_id)
            latest_message = chat.messages.latest('created_at')

            # Email notification
            notify_new_message(latest_message)

            # FCM push notification to the other participant
            recipient = chat.seller if latest_message.sender == chat.buyer else chat.buyer
            if FIREBASE_AVAILABLE and recipient.fcm_token:
                send_push_notification(
                    fcm_token=recipient.fcm_token,
                    title=f"New message from {latest_message.sender.username}",
                    body=latest_message.content[:100],
                    data={'type': 'new_message', 'chat_id': str(chat.id)}
                )
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
    Video/audio itself is carried by Agora, not this socket — this only
    relays viewer count, "now showing" product changes, reservations, likes,
    and stream lifecycle events. Anonymous viewing is allowed (same as
    AuctionConsumer); all writes happen over REST, this is broadcast-only.
    """

    async def connect(self):
        """Handle WebSocket connection"""
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.room_group_name = f'live_{self.session_id}'
        self.user = self.scope['user']

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        viewer_count = await self.increment_viewer_count()
        if viewer_count is not None:
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