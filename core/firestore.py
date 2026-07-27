"""
Firestore integration for realtime buyer/store chat.

Django/MySQL stays the source of truth for chat authorization (who is
buyer/seller/store). Chat and message rows are mirrored into Firestore so
clients can sign in with a Firebase custom token (see auth_views.py) and
read/write chat messages directly for realtime sync, without going through
the Django Channels WebSocket.

Currently paused (see settings.FIRESTORE_CHAT_SYNC_ENABLED) — live chat uses
the WebSocket transport instead while Firebase billing is disabled. All sync
functions below no-op silently while paused; flip the setting to reactivate.
"""
import logging
from django.conf import settings
from .firebase import get_firebase_app, FIREBASE_AVAILABLE

logger = logging.getLogger(__name__)

_firestore_client = None
FIRESTORE_AVAILABLE = False

try:
    from firebase_admin import firestore
    FIRESTORE_AVAILABLE = True
except ImportError:
    logger.warning("firebase-admin firestore module not available. Firestore chat sync disabled.")


def get_firestore_client():
    """Initialize Firestore client (singleton)"""
    global _firestore_client

    if not getattr(settings, 'FIRESTORE_CHAT_SYNC_ENABLED', False):
        return None

    if not FIRESTORE_AVAILABLE or not FIREBASE_AVAILABLE:
        return None

    if _firestore_client is not None:
        return _firestore_client

    app = get_firebase_app()
    if app is None:
        return None

    try:
        _firestore_client = firestore.client(app)
        return _firestore_client
    except Exception as e:
        logger.error(f"Firestore client initialization failed: {e}")
        return None


def sync_chat_to_firestore(chat):
    """Mirror a Chat row into Firestore so Security Rules can authorize participants"""
    db = get_firestore_client()
    if db is None:
        return

    try:
        db.collection('chats').document(str(chat.id)).set({
            'buyer_id': str(chat.buyer_id),
            'seller_id': str(chat.seller_id),
            'store_id': str(chat.store_id) if chat.store_id else None,
            'item_id': str(chat.item_id) if chat.item_id else None,
            'is_active': chat.is_active,
            'updated_at': firestore.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        logger.error(f"Failed to sync chat {chat.id} to Firestore: {e}")


def sync_message_to_firestore(message):
    """Mirror a Message row into Firestore so it's visible to Firestore listeners immediately"""
    db = get_firestore_client()
    if db is None:
        return

    try:
        db.collection('chats').document(str(message.chat_id)) \
            .collection('messages').document(str(message.id)).set({
                'sender_id': str(message.sender_id) if message.sender_id else None,
                'content': message.content,
                'type': message.message_type,
                'is_read': message.is_read,
                'created_at': firestore.SERVER_TIMESTAMP,
            })
    except Exception as e:
        logger.error(f"Failed to sync message {message.id} to Firestore: {e}")


def sync_user_fcm_token(user):
    """Mirror the user's FCM token into Firestore so the Cloud Function can read it
    via the Admin SDK without calling back into Django."""
    db = get_firestore_client()
    if db is None:
        return

    try:
        db.collection('users').document(str(user.id)).set({
            'fcm_token': user.fcm_token,
        }, merge=True)
    except Exception as e:
        logger.error(f"Failed to sync fcm_token for user {user.id} to Firestore: {e}")
