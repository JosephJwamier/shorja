"""مال البيع بالجملة: متى يصل البائع.

البضاعة تبقى في مخزن البائع بعد أن يشتريها التاجر، فإن وصله ثمنها كلّه عند
الموافقة لم يبقَ ما يُلزمه بتسليمها. القاعدتان:

  متجر جملة   متجر وثّقته المنصة (شارة StoreBadge.KIND_WHOLESALE): يصله الثمن
              فوراً، والمنصة مسؤولة عنه.
  غيره        يُحجز الثمن ويُحرَّر قطعةً قطعة: حين تصل القطعة إلى زبون التاجر
              ويُحرَّر ثمنها، أو حين يؤكّد التاجر أنه استلمها.

والمحجوز ليس رصيداً لأحد: خُصم من التاجر ولم يُضَف للبائع بعد، كمال الحجوزات.

ما يمنع المال من أن يعلق:
  استرداد      يطلبه التاجر، ويوافق عليه البائع أو تقرّره المنصة: تعود القطع
               التي لم تُبَع إلى مخزون البائع وثمنها إلى التاجر.
  تسليم        يعلنه البائع («سلّمتُ الباقي»)، فإن لم يعترض التاجر خلال
               HANDOVER_DAYS أيام حُرِّر الباقي.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from ..models import ItemTransfer, ItemVariant, Reservation, StoreBadge, User
from ..wallet_utils import credit_earnings

HANDOVER_DAYS = 7

NOTICE = (
    'ثمن البيع بالجملة يُحجز لدى المنصة ولا يصلك فوراً: يُحرَّر لك ثمن كل قطعة '
    'حين تُباع وتُسلَّم أو حين يؤكّد المشتري استلامها. وإن كنت تاجر جملة موثوقاً '
    'فتواصل مع المنصة لتحويل متجرك إلى «متجر جملة» يصله الثمن مباشرة عند الموافقة.'
)


class WholesaleError(ValueError):
    pass


def is_wholesale_store(user):
    # Off the prefetched rows on a list.
    store = getattr(user, 'store', None)
    return store is not None and any(
        b.kind == StoreBadge.KIND_WHOLESALE and b.revoked_at is None for b in store.badges.all())


def needs_notice(user):
    """The first approval only, and never for a wholesale store."""
    return user.wholesale_notice_at is None and not is_wholesale_store(user)


def mark_notice_seen(user):
    User.objects.filter(pk=user.pk, wholesale_notice_at__isnull=True).update(
        wholesale_notice_at=timezone.now())


def lock(transfer):
    return ItemTransfer.objects.select_for_update().get(pk=transfer.pk)


def release_units(transfer, units):
    """Pays the seller for up to `units` held pieces. `transfer` is locked.
    Returns how many were paid for."""
    from .commission import platform_account

    units = min(units, transfer.held_quantity)
    if units <= 0:
        return 0
    before, after = transfer.released_quantity, transfer.released_quantity + units

    def commission_up_to(n):
        # Cumulative, so the parts always add up to exactly the whole.
        return (transfer.platform_commission_amount * n / transfer.quantity
                ).quantize(Decimal('0.01'))

    commission = commission_up_to(after) - commission_up_to(before)
    gross = transfer.wholesale_price_per_unit * units
    links = {'item': transfer.item, 'transfer': transfer}
    credit_earnings(transfer.from_user, gross - commission, 'payout', **links)
    credit_earnings(platform_account(), commission, 'commission', **links)

    transfer.released_quantity = after
    if transfer.held_quantity == 0:
        transfer.refund_requested_at = transfer.handover_claimed_at = None
    transfer.save(update_fields=['released_quantity', 'refund_requested_at', 'handover_claimed_at'])
    return units


def goods_left(item, units):
    """`units` of `item` reached whoever bought them. If `item` was itself
    bought wholesale, its first seller is owed for those pieces, and so on up
    the chain. Call inside transaction.atomic()."""
    seen = set()
    while item is not None and item.pk not in seen:
        seen.add(item.pk)
        transfer = (ItemTransfer.objects.select_for_update()
                    .filter(resulting_item=item).first())
        if transfer is None:
            return
        release_units(transfer, units)
        item = transfer.item


def _open(transfer):
    if transfer.status != 'active' or transfer.held_quantity == 0:
        raise WholesaleError('لا يوجد مبلغ محجوز في هذه الاتفاقية')


def _close_if_settled(transfer, status='completed'):
    if transfer.held_quantity == 0 and transfer.status == 'active':
        transfer.status = status
        now = timezone.now()
        transfer.completed_at = now if status == 'completed' else transfer.completed_at
        transfer.cancelled_at = now if status == 'cancelled' else transfer.cancelled_at
        transfer.save(update_fields=['status', 'completed_at', 'cancelled_at'])


@transaction.atomic
def confirm_received(transfer, units=None):
    """The reseller took `units` (default: the rest) out of the warehouse."""
    transfer = lock(transfer)
    _open(transfer)
    units = transfer.held_quantity if units is None else units
    if units < 1 or units > transfer.held_quantity:
        raise WholesaleError('الكمية بين 1 و%d' % transfer.held_quantity)
    release_units(transfer, units)
    goods_left(transfer.item, units)
    # Everything is with them now: custody, and the delivery pickup, follow.
    _close_if_settled(transfer)
    return transfer


@transaction.atomic
def request_refund(transfer):
    transfer = lock(transfer)
    _open(transfer)
    transfer.refund_requested_at = timezone.now()
    # An objection to the seller's claim as much as a request.
    transfer.handover_claimed_at = None
    transfer.save(update_fields=['refund_requested_at', 'handover_claimed_at'])
    _notify(transfer.from_user, transfer, 'طلب استرداد بيع بالجملة',
            'يطلب المشتري استرداد ثمن ما لم يستلمه من "%s" (%d قطعة). وافق أو تواصل مع الدعم.')
    return transfer


@transaction.atomic
def claim_handover(transfer):
    transfer = lock(transfer)
    _open(transfer)
    if transfer.refund_requested_at:
        raise WholesaleError('طلب المشتري استرداد المبلغ — تواصل مع الدعم للفصل بينكما')
    if transfer.handover_claimed_at is None:
        transfer.handover_claimed_at = timezone.now()
        transfer.save(update_fields=['handover_claimed_at'])
        _notify(transfer.to_user, transfer, 'تأكيد استلام بضاعة الجملة',
                'يقول البائع إنه سلّمك بقية "%s" (%d قطعة). أكّد الاستلام، أو اطلب الاسترداد '
                'خلال ' + str(HANDOVER_DAYS) + ' أيام إن لم تستلمها.')
    return transfer


@transaction.atomic
def refund_rest(transfer):
    """Gives the reseller back the price of every held piece they still have
    unsold, and those pieces go back to the seller's stock. A piece already
    sold to a customer stays held until that sale settles."""
    transfer = lock(transfer)
    _open(transfer)
    mine = transfer.resulting_item.default_variant if transfer.resulting_item_id else None
    mine = mine and ItemVariant.objects.select_for_update().get(pk=mine.pk)
    on_the_way = Reservation.objects.filter(
        item=transfer.resulting_item, payment_released_to_seller=False,
    ).filter(Q(status__in=('completed', 'disputed')) | Q(status='pending', representative__isnull=False)
             ).aggregate(n=Sum('quantity'))['n'] or 0
    units = min(transfer.held_quantity - on_the_way, mine.quantity if mine else 0)
    if units <= 0:
        raise WholesaleError('لا قطع غير مبيعة لاستردادها')
    mine.take_stock(units)
    theirs = transfer.variant or transfer.item.default_variant
    if theirs is not None:
        ItemVariant.objects.select_for_update().get(pk=theirs.pk).return_stock(units)
    credit_earnings(transfer.to_user, transfer.wholesale_price_per_unit * units, 'refund',
                    item=transfer.item, transfer=transfer)
    transfer.refunded_quantity += units
    transfer.refund_requested_at = transfer.handover_claimed_at = None
    transfer.save(update_fields=['refunded_quantity', 'refund_requested_at', 'handover_claimed_at'])
    _close_if_settled(transfer, 'completed' if transfer.released_quantity else 'cancelled')
    return transfer


def settle_claimed_handovers(now=None):
    """The seller said they handed it over and the reseller never objected."""
    now = now or timezone.now()
    done = 0
    for transfer in ItemTransfer.objects.filter(
            status='active', escrow=True, refund_requested_at__isnull=True,
            handover_claimed_at__lte=now - timedelta(days=HANDOVER_DAYS)):
        with transaction.atomic():
            locked = lock(transfer)
            if (locked.handover_claimed_at is None or locked.refund_requested_at
                    or locked.status != 'active' or locked.held_quantity == 0):
                continue
            confirm_received(locked)
            done += 1
    return done


def holds_others_goods(user):
    """Is `user` still storing goods someone else bought, or a side of a
    wholesale sale whose money is not settled?"""
    for transfer in ItemTransfer.objects.filter(
            status='active', from_user=user).select_related('resulting_item'):
        if transfer.held_quantity or transfer.resulting_item is None \
                or transfer.resulting_item.quantity > 0:
            return True
    return any(t.held_quantity for t in ItemTransfer.objects.filter(status='active', to_user=user))


def held_totals(user):
    """(held for this seller, net of commission; paid by this reseller)."""
    owed = paid = Decimal('0')
    for t in ItemTransfer.objects.filter(status='active', escrow=True, from_user=user):
        gross = t.held_amount
        owed += gross - (t.platform_commission_amount * t.held_quantity / t.quantity)
    for t in ItemTransfer.objects.filter(status='active', escrow=True, to_user=user):
        paid += t.held_amount
    return owed, paid


def _notify(user, transfer, title, body):
    from ..notifications import bidi_isolate, notify_push

    text = body % (bidi_isolate(transfer.item.title), transfer.held_quantity)
    transaction.on_commit(lambda: notify_push(
        user, title, text, data={'type': 'general', 'transfer_id': str(transfer.pk)}))
