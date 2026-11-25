"""حظر مستخدم لآخر في المحادثات. لا يمسّ الطلبات القائمة بينهما: النزاع والدعم
يبقيان مفتوحين."""
from django.db.models import Q

from ..models import UserBlock

# The same words whoever blocked whom: being blocked is not announced.
CANNOT_MESSAGE = 'لا يمكن إرسال رسائل في هذه المحادثة'


def blocked_between(a, b):
    return UserBlock.objects.filter(
        Q(blocker_id=a, blocked_id=b) | Q(blocker_id=b, blocked_id=a)).exists()


def has_blocked(blocker, blocked):
    return UserBlock.objects.filter(blocker_id=blocker, blocked_id=blocked).exists()
