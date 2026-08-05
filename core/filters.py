"""
Core app filter classes using django-filter
"""
import django_filters
from django_filters import rest_framework as filters
from django.db.models import Q

from .models import (
    Item, FixedPriceListing, AuctionListing, Reservation,
    ItemTransfer, Chat, Rating, Dispute, Report, Transaction, Category, UserStore, Location,
    ProductReview, StoreReview, LiveSession,
)


class LocationFilter(filters.FilterSet):
    """Filter for Location model"""
    location_type = filters.CharFilter(lookup_expr='exact')
    city = filters.CharFilter(lookup_expr='icontains')
    # المحافظة — fixed choice on the model, so an exact (case-insensitive)
    # match is reliable, unlike the free-text `city` field above.
    governorate = filters.CharFilter(field_name='state', lookup_expr='iexact')
    country = filters.CharFilter(lookup_expr='icontains')
    is_primary = filters.BooleanFilter()
    is_active = filters.BooleanFilter()

    class Meta:
        model = Location
        fields = ['location_type', 'city', 'country', 'is_primary', 'is_active']


class ItemFilter(filters.FilterSet):
    """Advanced filter for Item model"""
    title = filters.CharFilter(lookup_expr='icontains')
    description = filters.CharFilter(lookup_expr='icontains')
    min_price = filters.NumberFilter(method='filter_min_price')
    max_price = filters.NumberFilter(method='filter_max_price')
    condition = filters.ChoiceFilter(choices=Item.CONDITION_CHOICES if hasattr(Item, 'CONDITION_CHOICES') else [])
    status = filters.CharFilter(lookup_expr='exact')
    category = filters.ModelChoiceFilter(queryset=Category.objects.all())
    owner = filters.NumberFilter(field_name='owner__id')
    created_after = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_before = filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')
    city = filters.CharFilter(field_name='current_location__city', lookup_expr='icontains')
    governorate = filters.CharFilter(field_name='current_location__state', lookup_expr='iexact')
    country = filters.CharFilter(field_name='current_location__country', lookup_expr='icontains')
    has_location = filters.BooleanFilter(method='filter_has_location')
    is_daily_deal = filters.BooleanFilter(field_name='is_daily_deal')

    class Meta:
        model = Item
        fields = ['status', 'condition', 'category', 'owner', 'is_daily_deal']

    def filter_min_price(self, queryset, name, value):
        return queryset.filter(
            Q(fixed_listing__price__gte=value) | Q(auction_listing__current_price__gte=value)
        )

    def filter_max_price(self, queryset, name, value):
        return queryset.filter(
            Q(fixed_listing__price__lte=value) | Q(auction_listing__current_price__lte=value)
        )

    def filter_has_location(self, queryset, name, value):
        if value:
            return queryset.filter(current_location__isnull=False)
        return queryset.filter(current_location__isnull=True)


class FixedPriceListingFilter(filters.FilterSet):
    """Filter for FixedPriceListing model"""
    min_price = filters.NumberFilter(field_name='price', lookup_expr='gte')
    max_price = filters.NumberFilter(field_name='price', lookup_expr='lte')
    min_deposit = filters.NumberFilter(field_name='deposit_percentage', lookup_expr='gte')
    max_deposit = filters.NumberFilter(field_name='deposit_percentage', lookup_expr='lte')
    is_active = filters.BooleanFilter(field_name='is_active')
    # Category filters (traverse item → category)
    category = filters.NumberFilter(field_name='item__category__id')
    category_slug = filters.CharFilter(field_name='item__category__slug')
    # Item-level filters
    condition = filters.ChoiceFilter(
        field_name='item__condition',
        choices=Item.CONDITION_CHOICES if hasattr(Item, 'CONDITION_CHOICES') else [],
    )
    is_daily_deal = filters.BooleanFilter(field_name='item__is_daily_deal')
    city = filters.CharFilter(field_name='item__current_location__city', lookup_expr='icontains')
    has_discount = filters.BooleanFilter(method='filter_has_discount')

    class Meta:
        model = FixedPriceListing
        fields = ['is_active', 'category', 'condition', 'is_daily_deal']

    def filter_has_discount(self, queryset, name, value):
        from django.db.models import F
        if value:
            return queryset.filter(original_price__isnull=False, price__lt=F('original_price'))
        return queryset


class AuctionListingFilter(filters.FilterSet):
    """Filter for AuctionListing model"""
    status = filters.CharFilter(lookup_expr='exact')
    min_price = filters.NumberFilter(field_name='current_price', lookup_expr='gte')
    max_price = filters.NumberFilter(field_name='current_price', lookup_expr='lte')
    starts_after = filters.DateTimeFilter(field_name='start_time', lookup_expr='gte')
    ends_before = filters.DateTimeFilter(field_name='end_time', lookup_expr='lte')
    active_only = filters.BooleanFilter(method='filter_active_only')
    # Category filters (traverse item → category)
    category = filters.NumberFilter(field_name='item__category__id')
    category_slug = filters.CharFilter(field_name='item__category__slug')
    # Item-level filters
    condition = filters.ChoiceFilter(
        field_name='item__condition',
        choices=Item.CONDITION_CHOICES if hasattr(Item, 'CONDITION_CHOICES') else [],
    )
    city = filters.CharFilter(field_name='item__current_location__city', lookup_expr='icontains')
    # Seller username filters
    seller_username = filters.CharFilter(field_name='item__owner__username', lookup_expr='icontains')
    seller_username_exact = filters.CharFilter(field_name='item__owner__username', lookup_expr='exact')

    class Meta:
        model = AuctionListing
        fields = ['status', 'category', 'condition', 'seller_username', 'seller_username_exact']

    def filter_active_only(self, queryset, name, value):
        if value:
            from django.utils import timezone
            now = timezone.now()
            return queryset.filter(status='active', start_time__lte=now, end_time__gte=now)
        return queryset


class ReservationFilter(filters.FilterSet):
    """Filter for Reservation model"""
    status = filters.CharFilter(lookup_expr='exact')
    buyer = filters.NumberFilter(field_name='buyer__id')
    seller = filters.NumberFilter(field_name='item__owner__id')
    created_after = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    expires_before = filters.DateTimeFilter(field_name='expires_at', lookup_expr='lte')

    class Meta:
        model = Reservation
        fields = ['status', 'buyer']


class LiveSessionFilter(filters.FilterSet):
    """Filter for LiveSession (live shopping) discovery feed"""
    status = filters.CharFilter(lookup_expr='exact')
    store = filters.NumberFilter(field_name='store__id')

    class Meta:
        model = LiveSession
        fields = ['status', 'store']


class ItemTransferFilter(filters.FilterSet):
    """Filter for ItemTransfer (wholesale ownership transfers)"""
    status = filters.CharFilter(lookup_expr='exact')
    owner = filters.NumberFilter(field_name='from_user__id')
    reseller = filters.NumberFilter(field_name='to_user__id')
    item = filters.NumberFilter(field_name='item__id')

    class Meta:
        model = ItemTransfer
        fields = ['status', 'owner', 'reseller', 'item']


class RatingFilter(filters.FilterSet):
    """Filter for Rating model"""
    rated_user = filters.NumberFilter(field_name='rated_user__id')
    rater = filters.NumberFilter(field_name='rater__id')
    min_score = filters.NumberFilter(field_name='score', lookup_expr='gte')
    max_score = filters.NumberFilter(field_name='score', lookup_expr='lte')
    role = filters.CharFilter(lookup_expr='exact')

    class Meta:
        model = Rating
        fields = ['rated_user', 'rater', 'role']


class DisputeFilter(filters.FilterSet):
    """Filter for Dispute model"""
    status = filters.CharFilter(lookup_expr='exact')
    complainant = filters.NumberFilter(field_name='complainant__id')
    defendant = filters.NumberFilter(field_name='defendant__id')
    created_after = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')

    class Meta:
        model = Dispute
        fields = ['status', 'complainant', 'defendant']


class ReportFilter(filters.FilterSet):
    """Filter for Report model"""
    status = filters.CharFilter(lookup_expr='exact')
    reason = filters.CharFilter(lookup_expr='exact')
    target_type = filters.CharFilter(method='filter_target_type')
    created_after = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')

    class Meta:
        model = Report
        fields = ['status', 'reason']

    def filter_target_type(self, queryset, name, value):
        field = {'item': 'reported_item', 'store': 'reported_store', 'user': 'reported_user'}.get(value)
        return queryset.filter(**{f'{field}__isnull': False}) if field else queryset.none()


class TransactionFilter(filters.FilterSet):
    """Filter for Transaction model"""
    transaction_type = filters.CharFilter(lookup_expr='exact')
    user = filters.NumberFilter(field_name='user__id')
    min_amount = filters.NumberFilter(field_name='amount', lookup_expr='gte')
    max_amount = filters.NumberFilter(field_name='amount', lookup_expr='lte')
    created_after = filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_before = filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')

    class Meta:
        model = Transaction
        fields = ['transaction_type', 'user']


class UserStoreFilter(filters.FilterSet):
    """Filter for UserStore model"""
    name = filters.CharFilter(lookup_expr='icontains')
    city = filters.CharFilter(field_name='default_location__city', lookup_expr='icontains')
    governorate = filters.CharFilter(field_name='default_location__state', lookup_expr='iexact')
    country = filters.CharFilter(field_name='default_location__country', lookup_expr='icontains')

    class Meta:
        model = UserStore
        fields = ['name']


class ProductReviewFilter(filters.FilterSet):
    """Filter for ProductReview model"""
    item = filters.NumberFilter(field_name='item__id')
    reviewer = filters.NumberFilter(field_name='reviewer__id')
    min_score = filters.NumberFilter(field_name='score', lookup_expr='gte')
    max_score = filters.NumberFilter(field_name='score', lookup_expr='lte')

    class Meta:
        model = ProductReview
        fields = ['item', 'reviewer']


class StoreReviewFilter(filters.FilterSet):
    """Filter for StoreReview model"""
    store = filters.NumberFilter(field_name='store__id')
    reviewer = filters.NumberFilter(field_name='reviewer__id')
    min_score = filters.NumberFilter(field_name='score', lookup_expr='gte')
    max_score = filters.NumberFilter(field_name='score', lookup_expr='lte')

    class Meta:
        model = StoreReview
        fields = ['store', 'reviewer']

