"""من يُشترى منه الآن: حسابه فعّال، ومتجره (إن كان له متجر) مفتوح.

إغلاق المتجر مؤقتاً قرار صاحبه ألّا يبيع الآن؛ كان يُخفي صفحته وتبقى بضاعته
تُشترى. ما دُفع أو حُجز قبل الإغلاق يُكمل.
"""
from django.db.models import Q


def open_seller_q(prefix=''):
    p = prefix + '__' if prefix else ''
    return Q(**{p + 'is_active': True}) & (
        Q(**{p + 'store__isnull': True}) | Q(**{p + 'store__is_active': True}))


def is_open(user):
    store = getattr(user, 'store', None)
    return user.is_active and (store is None or store.is_active)
