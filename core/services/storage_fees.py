"""رسوم التخزين في اتفاقيات الجملة: يدفعها المشتري لصاحب المخزن دورياً.

كانت الرسوم تُعرض «مستحقة» ولا يحصّلها شيء. القواعد هنا:

  متى      عند نهاية كل دورة (next_storage_fee_due)، ما دامت الاتفاقية نشطة
           ومدّتها لم تنتهِ.
  كم       ItemTransfer.calculate_due_storage_fee.
  بلا بضاعة لا رسم: دورة انتهت والبضاعة كلّها مبيعة تُسجَّل «معفاة».
  بلا رصيد  تُسجَّل الدورة «متأخرة» مرّة واحدة، وتُعاد المحاولة كل يوم على
           السجلّ نفسه؛ ولا تتراكم دورات فوقها حتى تُدفع.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import ItemTransfer, StorageFeePayment
from ..wallet_utils import credit_earnings, debit_balance

# اتفاقية نُسيت سنوات لا تُخصم دوراتها كلّها في تشغيل واحد بلا حدّ.
MAX_PERIODS_PER_RUN = 24


def collect_due_storage_fees(transfer_id, now=None):
    """Charges every period already due on one agreement. Returns how many
    were paid. Safe to run twice: each period is settled under the row lock
    and moves next_storage_fee_due past itself."""
    now = now or timezone.now()
    paid = 0
    for _ in range(MAX_PERIODS_PER_RUN):
        outcome = _collect_one(transfer_id, now)
        paid += outcome == 'paid'
        if outcome in (None, 'overdue'):
            break
    return paid


@transaction.atomic
def _collect_one(transfer_id, now):
    transfer = ItemTransfer.objects.select_for_update().get(pk=transfer_id)
    due = transfer.next_storage_fee_due
    if (transfer.status != 'active' or transfer.storage_fee_type == 'none'
            or due is None or due > now):
        return None
    if transfer.agreement_end_date and due > transfer.agreement_end_date:
        transfer.next_storage_fee_due = None
        transfer.save(update_fields=['next_storage_fee_due'])
        return None

    period = timedelta(days=transfer.storage_fee_period_days)
    amount = transfer.calculate_due_storage_fee().quantize(Decimal('0.01'))
    stored = transfer.resulting_item is not None and transfer.resulting_item.quantity > 0
    payment, created = StorageFeePayment.objects.get_or_create(
        transfer=transfer, period_end=due,
        defaults={'amount': amount, 'period_start': due - period, 'due_date': due},
    )

    outcome = 'paid'
    if not stored or amount <= 0:
        outcome = 'waived'
    else:
        try:
            with transaction.atomic():
                debit_balance(transfer.to_user, amount, 'storage_fee',
                              item=transfer.item, transfer=transfer)
                credit_earnings(transfer.from_user, amount, 'storage_fee',
                                item=transfer.item, transfer=transfer)
        except ValueError:
            payment.status = 'overdue'
            payment.save(update_fields=['status'])
            if created:
                _notify_overdue(transfer, amount)
            return 'overdue'
        transfer.total_storage_fees_paid += amount

    payment.status = outcome
    payment.amount = amount
    payment.paid_at = now if outcome == 'paid' else None
    payment.save(update_fields=['status', 'amount', 'paid_at'])
    transfer.next_storage_fee_due = due + period
    transfer.save(update_fields=['total_storage_fees_paid', 'next_storage_fee_due'])
    return outcome


def _notify_overdue(transfer, amount):
    from ..notifications import bidi_isolate, notify_push

    def send():
        notify_push(
            transfer.to_user, 'رسوم تخزين متأخرة',
            'رصيدك لا يكفي لرسوم تخزين "%s" (%s د.ع). اشحن محفظتك لتُسدَّد.'
            % (bidi_isolate(transfer.item.title), '{:,}'.format(int(amount))),
            data={'type': 'general', 'transfer_id': str(transfer.pk)},
        )
    transaction.on_commit(send)
