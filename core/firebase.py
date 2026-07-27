"""
Firebase Admin SDK integration for push notifications (FCM)
"""
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

_firebase_app = None
FIREBASE_AVAILABLE = False

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    logger.warning("firebase-admin not installed. Push notifications disabled. Run: pip install firebase-admin")


def get_firebase_app():
    """Initialize Firebase app (singleton)"""
    global _firebase_app

    if not FIREBASE_AVAILABLE:
        return None

    if _firebase_app is not None:
        return _firebase_app

    try:
        cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None)
        if not cred_path:
            logger.warning("FIREBASE_CREDENTIALS_PATH not set in settings.")
            return None

        cred = credentials.Certificate(cred_path)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase app initialized successfully.")
        return _firebase_app

    except Exception as e:
        logger.error(f"Firebase initialization failed: {e}")
        return None


def send_push_notification(fcm_token, title, body, data=None):
    """
    Send a push notification to a single device.

    Args:
        fcm_token (str): The FCM device token
        title (str): Notification title
        body (str): Notification body text
        data (dict): Optional key-value payload (values must be strings)

    Returns:
        bool: True if sent successfully, False otherwise
    """
    if not FIREBASE_AVAILABLE:
        return False

    if not fcm_token:
        return False

    app = get_firebase_app()
    if app is None:
        return False

    try:
        # Ensure all data values are strings (FCM requirement)
        str_data = {k: str(v) for k, v in (data or {}).items()}

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=str_data,
            token=fcm_token,
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    sound='default',
                    click_action='FLUTTER_NOTIFICATION_CLICK',
                )
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound='default')
                )
            ),
        )

        response = messaging.send(message)
        logger.info(f"FCM notification sent: {response}")
        return True

    except messaging.UnregisteredError:
        logger.warning(f"FCM token unregistered, clearing token.")
        _clear_invalid_token(fcm_token)
        return False

    except Exception as e:
        logger.error(f"FCM send error: {e}")
        return False


def send_push_to_multiple(fcm_tokens, title, body, data=None):
    """
    Send push notification to multiple devices.

    Args:
        fcm_tokens (list): List of FCM device tokens
        title (str): Notification title
        body (str): Notification body text
        data (dict): Optional key-value payload

    Returns:
        int: Number of successfully sent notifications
    """
    if not FIREBASE_AVAILABLE or not fcm_tokens:
        return 0

    app = get_firebase_app()
    if app is None:
        return 0

    try:
        str_data = {k: str(v) for k, v in (data or {}).items()}

        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=str_data,
            tokens=fcm_tokens,
            android=messaging.AndroidConfig(priority='high'),
        )

        response = messaging.send_each_for_multicast(message)
        logger.info(f"FCM multicast: {response.success_count} sent, {response.failure_count} failed")
        return response.success_count

    except Exception as e:
        logger.error(f"FCM multicast error: {e}")
        return 0


def _clear_invalid_token(fcm_token):
    """Remove invalid FCM token from the user's record"""
    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        User.objects.filter(fcm_token=fcm_token).update(fcm_token=None)
    except Exception as e:
        logger.error(f"Failed to clear invalid FCM token: {e}")
