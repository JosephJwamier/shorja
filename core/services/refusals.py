"""من يكرّر رفض الاستلام: يدفع كاملاً قبل الشحن.

البائع الذي لا يطلب عربوناً يتحمّل قراره، لكن من جعل الرفض عادةً لا يبقى له أن
يطلب من كل متجر بضاعةً يشحنها على حسابه. لا يُحسب رفضٌ ثبت أن صاحبه محقّ فيه
(نزاع حُسم بردّ المبلغ له).
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import DeliveryOrder, Dispute

FULL_PAYMENT_ONLY = (
    'رفضتَ استلام عدة طلبات مؤخراً، لذا يلزم دفع ثمن هذا الطلب كاملاً من المحفظة قبل شحنه.'
)


def deposit_refused_to(user):
    refused = DeliveryOrder.objects.filter(
        direction='outbound', reservation__buyer=user,
        refused_at__gte=timezone.now() - timedelta(days=settings.REFUSALS_WINDOW_DAYS),
    ).exclude(reservation__in=Dispute.objects.filter(
        complainant=user, status__in=('resolved', 'closed'),
        resolution_action__in=('refund_buyer', 'partial_refund'), reservation__isnull=False,
    ).values('reservation'))
    return refused.count() >= settings.REFUSALS_BEFORE_FULL_PAYMENT
