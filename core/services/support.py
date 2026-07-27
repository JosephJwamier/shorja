"""
"التواصل مع الدعم" — every user gets exactly one Chat with the fixed
support account created by migration 0041_create_support_account. Shared
by the DRF endpoint (ChatViewSet.create_or_get_support) so there is one
place that decides what "starting a support conversation" means.

Staff reply through core/admin.py's ChatAdmin.reply_view, not by logging
into the support account itself — Message.sender there is the real staff
member, while Chat.seller stays this one fixed identity.
"""
from django.core.exceptions import ImproperlyConfigured

from ..firestore import sync_chat_to_firestore, sync_message_to_firestore
from ..models import Chat, Message, User

SUPPORT_USERNAME = 'الدعم_الفني'


def get_support_account():
    try:
        return User.objects.get(username=SUPPORT_USERNAME)
    except User.DoesNotExist:
        raise ImproperlyConfigured(
            'Support account missing — run migrations '
            '(core.0041_create_support_account).'
        )


def start_support_chat(user, *, initial_message=''):
    """Get the user's existing support chat, or create it. Returns
    (chat, created)."""
    support_account = get_support_account()
    chat, created = Chat.objects.get_or_create(
        item=None, store=None, buyer=user, seller=support_account,
    )

    if chat.is_archived_by_buyer:
        chat.is_archived_by_buyer = False
        chat.save(update_fields=['is_archived_by_buyer'])

    if created and initial_message:
        message = Message.objects.create(
            chat=chat, sender=user, message_type='user', content=initial_message,
        )
        sync_message_to_firestore(message)

    sync_chat_to_firestore(chat)
    return chat, created
