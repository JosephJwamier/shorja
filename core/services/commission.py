"""Platform commission — what الشورجة keeps from each sale.

Rules, in one place:

  rate      Category.commission_percent, inherited up the tree, else
            settings.PLATFORM_COMMISSION_PERCENT. Read via
            Category.effective_commission_percent().
  multiplier  The seller's badge: founder × 0, pioneer × 0.5 (StoreBadge.
            COMMISSION_MULTIPLIER). The lowest multiplier a store holds wins.
  base      The reservation's total_price (after any coupon / negotiated
            price — the money that actually changes hands for the goods).
            Delivery fee is never commissioned; it is the courier's.
  when      Frozen on the reservation at creation (like the price), so a
            later rate change never touches an existing order. Taken from the
            seller's share at *release* — the only moment money leaves
            escrow — and credited to the platform account so it is a real,
            auditable balance and not money that vanished.
  who pays  The seller. The buyer sees and pays the listed price.

A second, independent commission is taken from the DELIVERY COMPANY out of
its delivery fee — see delivery_commission_for below. The two never
interact: the courier's fee is removed from the pot before the seller's
side is computed (wallet_utils.release_reservation_payment), so raising one
rate cannot move the other's money.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model

from ..models import StoreBadge

TWO_PLACES = Decimal('0.01')


def platform_account():
    """The user whose wallet receives commissions. Created on first use with
    the username from settings.PLATFORM_ACCOUNT_USERNAME; it is a system row
    (inactive for login) — nobody signs in as the platform."""
    User = get_user_model()
    username = getattr(settings, 'PLATFORM_ACCOUNT_USERNAME', 'platform')
    account, created = User.objects.get_or_create(
        username=username,
        defaults={'is_active': False, 'first_name': 'الشورجة', 'last_name': 'المنصّة'},
    )
    return account


def badge_multiplier(store):
    """The kindest multiplier among the store's active badges; 1 when none."""
    if store is None:
        return Decimal('1')
    kinds = set(store.badges.filter(revoked_at__isnull=True).values_list('kind', flat=True))
    multipliers = [StoreBadge.COMMISSION_MULTIPLIER[k]
                   for k in kinds if k in StoreBadge.COMMISSION_MULTIPLIER]
    return min(multipliers) if multipliers else Decimal('1')


def commission_for(item, total_price):
    """(percent, amount) the platform will take from this sale's seller.

    `percent` is the effective rate *after* the badge multiplier, so what is
    frozen on the reservation is exactly what will be charged.
    """
    rate = item.category.effective_commission_percent() if item.category_id else Decimal('0')
    store = getattr(item.owner, 'store', None)
    effective = (Decimal(rate) * badge_multiplier(store)).quantize(TWO_PLACES, ROUND_HALF_UP)
    amount = (Decimal(total_price) * effective / Decimal('100')).quantize(TWO_PLACES, ROUND_HALF_UP)
    return effective, amount


def freeze_on_reservation(reservation):
    """Sets the two frozen fields on a not-yet-saved (or just-created)
    reservation. Idempotent."""
    percent, amount = commission_for(reservation.item, reservation.total_price)
    reservation.platform_commission_percent = percent
    reservation.platform_commission_amount = amount
    return percent, amount


def delivery_commission_for(company, delivery_fee):
    """(percent, amount) الشورجة keeps out of a delivery company's fee.

    Negotiated per company (DeliveryCompany.platform_commission_percent);
    there is deliberately no platform-wide fallback, so a company whose
    terms were never set keeps its whole fee instead of being charged a
    rate nobody agreed with it.

    Unlike the seller's commission there is no badge multiplier: badges are
    a seller-loyalty device and have nothing to do with couriers.
    """
    fee = Decimal(delivery_fee or 0)
    if company is None or fee <= 0:
        return Decimal('0.00'), Decimal('0.00')
    percent = Decimal(company.platform_commission_percent or 0).quantize(TWO_PLACES, ROUND_HALF_UP)
    amount = (fee * percent / Decimal('100')).quantize(TWO_PLACES, ROUND_HALF_UP)
    return percent, amount
