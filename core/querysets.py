"""What the nested serializers read, fetched once per page instead of per row.

Lists only: a prefetched relation is a snapshot, and the actions that move
money must read theirs fresh.
"""
from django.db.models import Count, Exists, F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from rest_framework.filters import OrderingFilter


def user_related(queryset, *paths):
    """UserSerializer shows the user's store name and badges."""
    for path in paths:
        queryset = queryset.select_related(path, path + '__store').prefetch_related(
            path + '__store__badges')
    return queryset


def item_related(queryset, prefix=''):
    """Everything ItemSerializer reads of an item."""
    p = prefix + '__' if prefix else ''
    queryset = queryset.select_related(
        *([prefix] if prefix else []), p + 'category', p + 'fixed_listing', p + 'auction_listing',
        p + 'current_location')
    queryset = user_related(queryset, p + 'owner', p + 'current_holder', p + 'current_location__user')
    return queryset.prefetch_related(
        p + 'images', p + 'variants__image', p + 'location_history__location__user__store__badges',
        p + 'location_history__moved_by__store__badges')


def reservation_related(queryset, prefix=''):
    """Everything ReservationSerializer reads of a reservation."""
    p = prefix + '__' if prefix else ''
    queryset = queryset.select_related(
        *([prefix] if prefix else []), p + 'applied_coupon__coupon', p + 'delivery_location')
    queryset = user_related(queryset, p + 'buyer', p + 'representative', p + 'delivery_location__user')
    return item_related(queryset, p + 'item').prefetch_related(
        p + 'delivery_orders__company', p + 'disputes', p + 'installment_plans__collections')


def with_store_numbers(queryset, viewer=None):
    """The counts UserStoreSerializer shows, as one subquery each on the page's
    own SELECT. Read back by the serializer as `n_*`."""
    from .models import Item, LiveSession, StoreFollow, StoreReview

    def count(inner, owner_field):
        return Coalesce(Subquery(
            inner.order_by().values(owner_field).annotate(n=Count('pk')).values('n')[:1]), Value(0))

    live_status = ['active', 'reserved']
    queryset = queryset.annotate(
        n_items=count(Item.objects.filter(owner=OuterRef('user')), 'owner'),
        n_owned_active=count(Item.objects.filter(
            owner=OuterRef('user'), status__in=live_status), 'owner'),
        n_held_active=count(Item.objects.filter(
            current_holder=OuterRef('user'), status__in=live_status).exclude(
                owner=F('current_holder')), 'current_holder'),
        n_reviews=count(StoreReview.objects.filter(store=OuterRef('pk')), 'store'),
        n_positive=count(StoreReview.objects.filter(store=OuterRef('pk'), score__gte=4), 'store'),
        n_followers=count(StoreFollow.objects.filter(store=OuterRef('pk')), 'store'),
        n_live_id=Subquery(LiveSession.objects.filter(
            store=OuterRef('pk'), status='live').order_by('pk').values('pk')[:1]),
    )
    if viewer is not None and viewer.is_authenticated:
        queryset = queryset.annotate(n_following=Exists(
            StoreFollow.objects.filter(store=OuterRef('pk'), follower=viewer)))
    return queryset.prefetch_related('badges', 'user__store__badges')


class StableOrderingFilter(OrderingFilter):
    """Ends every order in the id. Rows equal in what is sorted by (the same
    price, the same rating) come back in any order the database likes, and a
    page cut through them shows one twice and another never."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if ordering and not {'id', '-id', 'pk', '-pk'} & set(ordering):
            ordering = [*ordering, '-id']
        return ordering
