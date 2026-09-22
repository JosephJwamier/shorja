"""Store badges: who gets what, and the one list the app renders.

Two kinds of badge live side by side:

  granted   StoreBadge rows. founder / pioneer / certified come only from an
            admin (capped); `verified` is granted here automatically once the
            owner's identity and phone are both verified, and revoked again if
            either verification is withdrawn.
  derived   UserStore.level (bronze → diamond), recomputed from the store's
            last-90-day confirmed sales, rating and followers. Never granted,
            never revoked — it simply is what the numbers say.

`badges_for_store` merges both into the display order the app expects.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from ..models import Reservation, StoreBadge, StoreFollow, UserStore

# Display order: earned status first, then the performance level.
DISPLAY_ORDER = [
    StoreBadge.KIND_FOUNDER, StoreBadge.KIND_PIONEER, StoreBadge.KIND_CERTIFIED,
    StoreBadge.KIND_VERIFIED,
]

LEVEL_WINDOW_DAYS = 90

# Thresholds a store must meet for each level (all conditions at once).
# Overridable from settings.STORE_LEVEL_THRESHOLDS so they can be tuned
# without a deploy — see the summary given to the owner on 2026-09-17.
DEFAULT_LEVEL_THRESHOLDS = {
    UserStore.LEVEL_BRONZE:  {'sales': 1,   'rating': Decimal('0'),   'followers': 0},
    UserStore.LEVEL_SILVER:  {'sales': 20,  'rating': Decimal('4.0'), 'followers': 0},
    UserStore.LEVEL_GOLD:    {'sales': 100, 'rating': Decimal('4.3'), 'followers': 100},
    UserStore.LEVEL_DIAMOND: {'sales': 400, 'rating': Decimal('4.6'), 'followers': 500},
}
LEVEL_LADDER = [UserStore.LEVEL_BRONZE, UserStore.LEVEL_SILVER, UserStore.LEVEL_GOLD,
                UserStore.LEVEL_DIAMOND]


class BadgeError(Exception):
    pass


# ---------------------------------------------------------------------------
# Granted badges
# ---------------------------------------------------------------------------

def active_badges(store):
    return store.badges.filter(revoked_at__isnull=True)


def grant_badge(store, kind, *, by=None, note=''):
    """Gives `store` the badge, enforcing the caps. Idempotent: an already
    active badge of that kind is returned unchanged."""
    if kind not in dict(StoreBadge.KIND_CHOICES):
        raise BadgeError('unknown badge kind %r' % kind)
    if kind in StoreBadge.ADMIN_ONLY and by is None:
        raise BadgeError('%s is granted by an admin only' % kind)
    with transaction.atomic():
        existing = active_badges(store).filter(kind=kind).first()
        if existing:
            return existing
        cap = StoreBadge.CAPS.get(kind)
        if cap is not None:
            # Lock the kind's rows so two admins cannot both grant the 50th.
            taken = (StoreBadge.objects.select_for_update()
                     .filter(kind=kind, revoked_at__isnull=True).count())
            if taken >= cap:
                raise BadgeError('اكتمل عدد شارات «%s» (%d)' % (dict(StoreBadge.KIND_CHOICES)[kind], cap))
        return StoreBadge.objects.create(store=store, kind=kind, granted_by=by, note=note)


def revoke_badge(store, kind, *, by=None, note=''):
    badge = active_badges(store).filter(kind=kind).first()
    if badge is None:
        return None
    badge.revoked_at = timezone.now()
    badge.revoked_by = by
    if note:
        badge.note = (badge.note + ' | ' if badge.note else '') + note
    badge.save(update_fields=['revoked_at', 'revoked_by', 'note'])
    return badge


def sync_verified_badge(store):
    """`verified` follows the owner's verification flags exactly."""
    user = store.user
    qualifies = bool(user.identity_verified and user.phone_verified)
    has = active_badges(store).filter(kind=StoreBadge.KIND_VERIFIED).exists()
    if qualifies and not has:
        return grant_badge(store, StoreBadge.KIND_VERIFIED, note='هوية وهاتف موثّقان')
    if has and not qualifies:
        return revoke_badge(store, StoreBadge.KIND_VERIFIED, note='أُلغي التوثيق')
    return None


# ---------------------------------------------------------------------------
# Derived level
# ---------------------------------------------------------------------------

def level_thresholds():
    merged = {k: dict(v) for k, v in DEFAULT_LEVEL_THRESHOLDS.items()}
    for level, override in (getattr(settings, 'STORE_LEVEL_THRESHOLDS', None) or {}).items():
        merged.setdefault(level, {}).update(override)
    return merged


def store_stats(store, *, now=None):
    """The three numbers the level is decided on."""
    now = now or timezone.now()
    since = now - timedelta(days=LEVEL_WINDOW_DAYS)
    sales = Reservation.objects.filter(
        item__owner=store.user, status='confirmed', payment_released_to_seller=True,
        payment_released_at__gte=since,
    ).count()
    followers = StoreFollow.objects.filter(store=store).count()
    return {'sales': sales, 'rating': store.rating or Decimal('0'), 'followers': followers}


def level_for(stats, thresholds=None):
    """Highest level whose every threshold the stats meet; '' when none."""
    thresholds = thresholds or level_thresholds()
    reached = UserStore.LEVEL_NONE
    for level in LEVEL_LADDER:
        need = thresholds[level]
        if (stats['sales'] >= need['sales'] and stats['rating'] >= need['rating']
                and stats['followers'] >= need['followers']):
            reached = level
        else:
            break  # the ladder is monotonic: a missed rung stops the climb
    return reached


def recompute_level(store, *, now=None):
    """Recomputes and stores the level. Returns (old, new)."""
    old = store.level
    new = level_for(store_stats(store, now=now))
    if new != old or store.level_updated_at is None:
        store.level = new
        store.level_updated_at = now or timezone.now()
        store.save(update_fields=['level', 'level_updated_at'])
    return old, new


# ---------------------------------------------------------------------------
# What the app shows
# ---------------------------------------------------------------------------

def badges_for_store(store):
    """Ordered badge kinds: granted ones (display order) then the level."""
    kinds = set(active_badges(store).values_list('kind', flat=True))
    out = [k for k in DISPLAY_ORDER if k in kinds]
    if store.level:
        out.append(store.level)
    return out


def badges_for_stores(stores):
    """Same as badges_for_store for a list, in two queries instead of 2N."""
    ids = [s.id for s in stores]
    granted = {}
    for store_id, kind in (StoreBadge.objects
                           .filter(store_id__in=ids, revoked_at__isnull=True)
                           .values_list('store_id', 'kind')):
        granted.setdefault(store_id, set()).add(kind)
    result = {}
    for store in stores:
        kinds = granted.get(store.id, set())
        out = [k for k in DISPLAY_ORDER if k in kinds]
        if store.level:
            out.append(store.level)
        result[store.id] = out
    return result


# ---------------------------------------------------------------------------
# Reward coupons — the platform's thank-you to a badged store
# ---------------------------------------------------------------------------

REWARD_COUPON = {
    'amount': Decimal('5000.00'),
    'min_order_amount': Decimal('50000.00'),
    'days': 30,
}


def grant_reward_coupon(store, *, by=None, amount=None, min_order_amount=None, days=None):
    """Creates a collectable StoreCoupon for `store`'s customers.

    It is a normal store coupon (the store bears the discount exactly as
    with coupons it creates itself); what makes it a reward is who created
    it and the `description` that says so. Returns None when the store
    already has an active, unexpired reward coupon, so re-running the admin
    action does not stack them.
    """
    import secrets
    from ..models import StoreCoupon

    now = timezone.now()
    if StoreCoupon.objects.filter(
        store=store, is_active=True, description__startswith='مكافأة',
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)).exists():
        return None
    amount = amount if amount is not None else REWARD_COUPON['amount']
    min_order = min_order_amount if min_order_amount is not None else REWARD_COUPON['min_order_amount']
    days = days if days is not None else REWARD_COUPON['days']
    code = 'GIFT-' + secrets.token_hex(3).upper()
    while StoreCoupon.objects.filter(store=store, code=code).exists():
        code = 'GIFT-' + secrets.token_hex(3).upper()
    return StoreCoupon.objects.create(
        store=store, code=code, amount=amount, min_order_amount=min_order,
        description='مكافأة من الشورجة لزبائن %s' % store.name,
        expires_at=now + timedelta(days=days),
    )
