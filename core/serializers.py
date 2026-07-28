"""
ReSell Trust - Django REST Framework Serializers
ENHANCED: Updated to support new Location tracking, Storage Fees, and Transfer features
"""
from django.utils import timezone
from decimal import Decimal
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Item, ItemImage, FixedPriceListing, AuctionListing,
    Bid, Reservation, Representative, ItemTransfer, Category,
    UserStore, Rating, Transaction, Chat, Message, Dispute,
    Location, ItemLocationHistory, StorageFeePayment, Notification,
    ProductReview, StoreReview, StoreFollow, StoreCoupon, CouponCollection,
    LiveSession, LiveSessionItem, LiveSessionComment,
)
from .services.support import SUPPORT_USERNAME

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """User serializer"""

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'phone', 'trust_score',
                  'total_sales', 'total_purchases', 'email_verified',
                  'phone_verified', 'identity_verified', 'date_joined']
        read_only_fields = ['trust_score', 'total_sales', 'total_purchases',
                            'email_verified', 'phone_verified', 'identity_verified']

    def update(self, instance, validated_data):
        # Changing the verified address means the old verification no longer
        # vouches for it — require re-verification.
        if 'email' in validated_data and validated_data['email'] != instance.email:
            instance.email_verified = False
        if 'phone' in validated_data and validated_data['phone'] != instance.phone:
            instance.phone_verified = False
        return super().update(instance, validated_data)


# NEW: Location Serializers
class LocationSerializer(serializers.ModelSerializer):
    """Location serializer"""
    user = UserSerializer(read_only=True)
    full_address = serializers.SerializerMethodField()

    class Meta:
        model = Location
        fields = [
            'id', 'user', 'location_type', 'name', 'address_line1',
            'address_line2', 'city', 'state', 'postal_code', 'country',
            'latitude', 'longitude', 'contact_person', 'contact_phone',
            'notes', 'is_primary', 'is_active', 'full_address',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'created_at', 'updated_at']

    def get_full_address(self, obj):
        return obj.get_full_address()


class LocationCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating locations"""

    class Meta:
        model = Location
        fields = [
            'location_type', 'name', 'address_line1', 'address_line2',
            'city', 'state', 'postal_code', 'country', 'latitude',
            'longitude', 'contact_person', 'contact_phone', 'notes',
            'is_primary', 'is_active'
        ]

    def validate(self, data):
        """Ensure only one primary location per user"""
        user = self.context.get('request').user
        is_primary = data.get('is_primary', False)

        if is_primary:
            # If setting as primary, check if updating existing or creating new
            instance = self.instance
            if instance:
                # Updating - unset other primary locations
                Location.objects.filter(
                    user=user,
                    is_primary=True
                ).exclude(id=instance.id).update(is_primary=False)
            else:
                # Creating new - unset all other primary locations
                Location.objects.filter(
                    user=user,
                    is_primary=True
                ).update(is_primary=False)

        return data


class ItemLocationHistorySerializer(serializers.ModelSerializer):
    """Item location history serializer"""
    location = LocationSerializer(read_only=True)
    moved_by = UserSerializer(read_only=True)

    class Meta:
        model = ItemLocationHistory
        fields = ['id', 'item', 'location', 'moved_by', 'moved_at', 'notes']
        read_only_fields = ['item', 'location', 'moved_by', 'moved_at']


class CategorySerializer(serializers.ModelSerializer):
    """Category serializer"""
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            'id', 'name', 'name_ar', 'slug', 'parent', 'icon',
            'image', 'image_url', 'order', 'is_active',
        ]
        extra_kwargs = {'image': {'write_only': True}}

    def get_image_url(self, obj):
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None


class ItemImageSerializer(serializers.ModelSerializer):
    """Item image serializer"""

    class Meta:
        model = ItemImage
        fields = ['id', 'image', 'order', 'created_at']


class ItemListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for home screen item cards"""
    name = serializers.CharField(source='title')
    image = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    original_price = serializers.SerializerMethodField()
    discount_percentage = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            'id', 'name', 'image', 'price', 'original_price', 'discount_percentage',
            'sold_count', 'rating', 'currency',
        ]

    def get_image(self, obj):
        first_image = obj.images.order_by('order').first()
        if not first_image:
            return None
        request = self.context.get('request')
        url = first_image.image.url
        return request.build_absolute_uri(url) if request else url

    def get_price(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        if fixed_listing:
            return fixed_listing.price
        auction_listing = getattr(obj, 'auction_listing', None)
        if auction_listing:
            return auction_listing.current_price
        return None

    def get_original_price(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        return fixed_listing.original_price if fixed_listing else None

    def get_discount_percentage(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        return fixed_listing.discount_percentage if fixed_listing else 0

    def get_currency(self, obj):
        return 'IQD'


class ItemSerializer(serializers.ModelSerializer):
    """Item serializer - ENHANCED with location tracking"""
    owner = UserSerializer(read_only=True)
    current_holder = UserSerializer(read_only=True)
    images = ItemImageSerializer(many=True, read_only=True)
    category = CategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        source='category',
        write_only=True
    )
    current_location = LocationSerializer(read_only=True)
    location_history = ItemLocationHistorySerializer(many=True, read_only=True)
    distance_km = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    original_price = serializers.SerializerMethodField()
    discount_percentage = serializers.SerializerMethodField()
    total_reviews = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            'id', 'owner', 'current_holder', 'title', 'name_ar', 'description',
            'category', 'category_id', 'condition', 'quantity', 'status',
            'package_size',
            'price', 'original_price', 'discount_percentage',
            'rating', 'sold_count', 'total_reviews',
            'is_daily_deal',
            'current_location', 'location_history', 'images',
            'distance_km', 'created_at', 'updated_at'
        ]
        read_only_fields = ['owner', 'current_holder', 'status', 'current_location',
                            'rating', 'sold_count']

    def get_price(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        if fixed_listing:
            return fixed_listing.price
        auction_listing = getattr(obj, 'auction_listing', None)
        if auction_listing:
            return auction_listing.current_price
        return None

    def get_original_price(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        return fixed_listing.original_price if fixed_listing else None

    def get_discount_percentage(self, obj):
        fixed_listing = getattr(obj, 'fixed_listing', None)
        return fixed_listing.discount_percentage if fixed_listing else 0

    def get_total_reviews(self, obj):
        return obj.product_reviews.count()

    def get_distance_km(self, obj):
        return getattr(obj, 'distance_km', None)


class ItemCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating items"""
    current_location_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = Item
        fields = ['title', 'description', 'category', 'condition', 'quantity',
                  'package_size', 'current_location_id']

    def validate_current_location_id(self, value):
        if value:
            request = self.context.get('request')
            try:
                Location.objects.get(id=value, user=request.user)
                return value
            except Location.DoesNotExist:
                raise serializers.ValidationError("Location not found or doesn't belong to you")
        return value

    def create(self, validated_data):
        current_location_id = validated_data.pop('current_location_id', None)
        item = super().create(validated_data)
        if current_location_id:
            location = Location.objects.get(id=current_location_id)
            item.current_location = location
            item.save(update_fields=['current_location'])
        return item


class ItemUpdateLocationSerializer(serializers.Serializer):
    """Serializer for updating item location"""
    current_location_id = serializers.IntegerField()
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_current_location_id(self, value):
        request = self.context.get('request')
        try:
            Location.objects.get(id=value)
            item = self.context.get('item')
            if item and request.user not in [item.owner, item.current_holder]:
                raise serializers.ValidationError("You don't have permission to move this item")
            return value
        except Location.DoesNotExist:
            raise serializers.ValidationError("Location not found")


class FixedPriceListingCardSerializer(serializers.ModelSerializer):
    """Lightweight card for category browse & home screen"""
    name = serializers.CharField(source='item.title')
    name_ar = serializers.CharField(source='item.name_ar')
    image = serializers.SerializerMethodField()
    discount_percentage = serializers.SerializerMethodField()
    sold_count = serializers.IntegerField(source='item.sold_count')
    rating = serializers.DecimalField(source='item.rating', max_digits=3, decimal_places=2)
    condition = serializers.CharField(source='item.condition')
    category = CategorySerializer(source='item.category', read_only=True)
    currency = serializers.SerializerMethodField()

    class Meta:
        model = FixedPriceListing
        fields = [
            'id', 'name', 'name_ar', 'image',
            'price', 'original_price', 'discount_percentage',
            'sold_count', 'rating', 'condition',
            'deposit_percentage', 'category', 'currency', 'created_at',
        ]

    def get_image(self, obj):
        first = obj.item.images.order_by('order').first()
        if not first:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(first.image.url) if request else first.image.url

    def get_discount_percentage(self, obj):
        return obj.discount_percentage

    def get_currency(self, obj):
        return 'IQD'


class AuctionListingCardSerializer(serializers.ModelSerializer):
    """Lightweight card for category browse & home screen"""
    name = serializers.CharField(source='item.title')
    name_ar = serializers.CharField(source='item.name_ar')
    image = serializers.SerializerMethodField()
    condition = serializers.CharField(source='item.condition')
    category = CategorySerializer(source='item.category', read_only=True)
    bid_count = serializers.SerializerMethodField()
    time_remaining_seconds = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = AuctionListing
        fields = [
            'id', 'name', 'name_ar', 'image',
            'starting_price', 'current_price', 'minimum_increment',
            'bid_count', 'time_remaining_seconds',
            'condition', 'category', 'status',
            'start_time', 'end_time', 'currency',
        ]

    def get_image(self, obj):
        first = obj.item.images.order_by('order').first()
        if not first:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(first.image.url) if request else first.image.url

    def get_bid_count(self, obj):
        return obj.bids.count()

    def get_time_remaining_seconds(self, obj):
        if obj.status == 'active':
            delta = obj.end_time - timezone.now()
            return max(0, int(delta.total_seconds()))
        return 0

    def get_currency(self, obj):
        return 'IQD'


class FixedPriceListingSerializer(serializers.ModelSerializer):
    """Fixed price listing serializer — full detail"""
    item = ItemSerializer(read_only=True)
    discount_percentage = serializers.SerializerMethodField()

    class Meta:
        model = FixedPriceListing
        fields = ['id', 'item', 'price', 'original_price', 'discount_percentage',
                  'deposit_percentage', 'deposit_amount', 'is_active', 'created_at']
        read_only_fields = ['deposit_amount', 'item']

    def get_discount_percentage(self, obj):
        return obj.discount_percentage


class FixedPriceListingCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating fixed price listings"""

    class Meta:
        model = FixedPriceListing
        fields = ['price', 'original_price', 'deposit_percentage']
        extra_kwargs = {'original_price': {'required': False, 'allow_null': True}}

    def validate_deposit_percentage(self, value):
        """Ensure deposit percentage is between 0 and 100"""
        if value < 0 or value > 100:
            raise serializers.ValidationError("Deposit percentage must be between 0 and 100")
        return value

    def validate_price(self, value):
        """Ensure price is positive"""
        if value <= 0:
            raise serializers.ValidationError("Price must be greater than 0")
        return value

    def validate_original_price(self, value):
        """Ensure the reference price (if set) is positive"""
        if value is not None and value <= 0:
            raise serializers.ValidationError("Original price must be greater than 0")
        return value


class BidSerializer(serializers.ModelSerializer):
    """Auction bid serializer"""
    bidder = UserSerializer(read_only=True)

    class Meta:
        model = Bid
        fields = ['id', 'auction', 'bidder', 'amount', 'deposit_paid',
                  'is_winning', 'created_at']
        read_only_fields = ['deposit_paid', 'is_winning']

    def validate_amount(self, value):
        """Validate bid amount"""
        auction = self.context.get('auction')
        if not auction:
            return value

        if value < auction.current_price + auction.minimum_increment:
            raise serializers.ValidationError(
                f"Bid must be at least ${auction.current_price + auction.minimum_increment}"
            )

        return value


class CreateBidSerializer(serializers.Serializer):
    """Serializer for creating a bid"""
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Bid amount must be positive")
        return value


class AuctionListingSerializer(serializers.ModelSerializer):
    """Auction listing serializer"""
    item = ItemSerializer(read_only=True)
    winner = UserSerializer(read_only=True)
    bids = BidSerializer(many=True, read_only=True)
    bid_count = serializers.SerializerMethodField()
    time_remaining = serializers.SerializerMethodField()

    class Meta:
        model = AuctionListing
        fields = ['id', 'item', 'starting_price', 'current_price',
                  'minimum_increment', 'bid_deposit', 'start_time',
                  'end_time', 'status', 'winner', 'bids', 'bid_count',
                  'time_remaining', 'created_at']

    def get_bid_count(self, obj):
        return obj.bids.count()

    def get_time_remaining(self, obj):
        if obj.status == 'active':
            remaining = obj.end_time - timezone.now()
            return int(remaining.total_seconds())
        return 0


class AuctionListingCreateSerializer(serializers.ModelSerializer):
    """Create auction listing serializer"""

    class Meta:
        model = AuctionListing
        fields = [
            'starting_price',
            'minimum_increment',
            'bid_deposit',
            'start_time',
            'end_time',
        ]

    def validate_starting_price(self, value):
        if value <= Decimal('0'):
            raise serializers.ValidationError("Starting price must be greater than 0.")
        return value

    def validate_minimum_increment(self, value):
        if value <= Decimal('0'):
            raise serializers.ValidationError("Minimum increment must be greater than 0.")
        return value

    def validate_bid_deposit(self, value):
        if value < Decimal('0'):
            raise serializers.ValidationError("Bid deposit cannot be negative.")
        return value

    def validate(self, attrs):
        now = timezone.now()
        start_time = attrs.get('start_time')
        end_time = attrs.get('end_time')

        if start_time >= end_time:
            raise serializers.ValidationError(
                {"end_time": "End time must be after start time."}
            )

        if end_time <= now:
            raise serializers.ValidationError(
                {"end_time": "End time must be in the future."}
            )

        return attrs

    def create(self, validated_data):
        """Status is derived automatically"""
        now = timezone.now()
        start_time = validated_data['start_time']
        end_time = validated_data['end_time']

        if start_time > now:
            status = 'upcoming'
        elif start_time <= now < end_time:
            status = 'active'
        else:
            raise serializers.ValidationError("Auction end time has already passed.")

        validated_data['status'] = status
        validated_data['current_price'] = validated_data['starting_price']

        return super().create(validated_data)


# NEW: Storage Fee Payment Serializer
class StorageFeePaymentSerializer(serializers.ModelSerializer):
    """Storage fee payment serializer"""
    transfer = serializers.PrimaryKeyRelatedField(read_only=True)
    is_overdue = serializers.SerializerMethodField()
    days_until_due = serializers.SerializerMethodField()

    class Meta:
        model = StorageFeePayment
        fields = [
            'id', 'transfer', 'amount', 'period_start', 'period_end',
            'due_date', 'status', 'paid_at', 'is_overdue', 'days_until_due',
            'created_at'
        ]
        read_only_fields = ['transfer', 'created_at', 'paid_at']

    def get_is_overdue(self, obj):
        return obj.is_overdue()

    def get_days_until_due(self, obj):
        if obj.status == 'paid':
            return None
        delta = obj.due_date - timezone.now()
        return delta.days if delta.days > 0 else 0


class ReservationSerializer(serializers.ModelSerializer):
    """Reservation serializer - ENHANCED with transfer support"""
    item = ItemSerializer(read_only=True)
    buyer = UserSerializer(read_only=True)
    representative = UserSerializer(read_only=True)
    delivery_location = LocationSerializer(read_only=True)
    related_transfer = serializers.PrimaryKeyRelatedField(read_only=True)
    time_remaining = serializers.SerializerMethodField()
    has_dispute = serializers.SerializerMethodField()
    dispute_status = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    can_approve = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    coupon_code = serializers.SerializerMethodField()
    is_negotiated_price = serializers.SerializerMethodField()
    live_session = serializers.PrimaryKeyRelatedField(read_only=True)
    live_session_item = serializers.PrimaryKeyRelatedField(read_only=True)
    auction = serializers.PrimaryKeyRelatedField(read_only=True)
    proof_screenshot = serializers.SerializerMethodField()
    delivery_order = serializers.SerializerMethodField()
    can_send_to_delivery = serializers.SerializerMethodField()
    can_request_payment_release = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            'id', 'item', 'buyer', 'quantity', 'deposit_amount', 'total_price',
            'status', 'reserved_at', 'expires_at', 'completed_at',
            'is_deposit_only', 'amount_charged',
            'buyer_approved', 'buyer_approved_at', 'seller_cancellation_approved',
            'payment_released_to_seller', 'payment_released_at',
            'representative', 'representative_assignment', 'commission_amount',
            'commission_paid', 'commission_paid_at',
            'is_transfer_resale', 'related_transfer', 'original_owner_commission',
            'delivery_location', 'delivery_fee',
            'coupon_discount', 'coupon_code', 'is_negotiated_price',
            'live_session', 'live_session_item', 'auction', 'proof_screenshot',
            'time_remaining', 'has_dispute', 'dispute_status',
            'can_cancel', 'can_approve', 'payment_status',
            'delivery_order', 'can_send_to_delivery', 'can_request_payment_release',
        ]
        # Every one of these transitions has its own guarded action
        # (create_reservation/complete/cancel/approve_and_release_payment/...);
        # none of it should be settable through a plain PATCH.
        read_only_fields = [
            'quantity', 'deposit_amount', 'total_price', 'status',
            'reserved_at', 'expires_at', 'completed_at', 'is_deposit_only',
            'buyer_approved', 'buyer_approved_at', 'seller_cancellation_approved',
            'payment_released_to_seller', 'payment_released_at',
            'representative_assignment', 'commission_amount',
            'commission_paid', 'commission_paid_at',
            'is_transfer_resale', 'original_owner_commission',
            'coupon_discount', 'delivery_fee',
        ]

    def get_coupon_code(self, obj):
        if obj.applied_coupon_id:
            return obj.applied_coupon.coupon.code
        return None

    def get_proof_screenshot(self, obj):
        if not obj.proof_screenshot:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.proof_screenshot.url) if request else obj.proof_screenshot.url

    def get_is_negotiated_price(self, obj):
        return obj.applied_offer_id is not None

    def get_time_remaining(self, obj):
        if obj.status == 'pending':
            remaining = obj.expires_at - timezone.now()
            return int(remaining.total_seconds()) if remaining.total_seconds() > 0 else 0
        return 0

    def get_has_dispute(self, obj):
        return obj.disputes.exists()

    def get_dispute_status(self, obj):
        dispute = obj.disputes.order_by('-created_at').first()
        if dispute:
            return {
                'id': dispute.id,
                'status': dispute.status,
                'reason': dispute.reason,
                'title': dispute.title
            }
        return None

    def get_can_cancel(self, obj):
        """Mirrors ReservationViewSet.cancel's actual eligibility rules for
        whichever role (buyer or seller) the requesting user plays on this
        reservation, so the client can gate the cancel button per-user."""
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user is None or not user.is_authenticated:
            return False
        # 'confirmed' is the final settled state — no longer cancellable by
        # either side, matching ReservationViewSet.cancel.
        if obj.status not in ('pending', 'completed'):
            return False
        if obj.buyer_id == user.id:
            return obj.can_buyer_cancel()
        if obj.item.owner_id == user.id or obj.item.current_holder_id == user.id:
            # Not once the delivery company has already picked the item up
            # from the seller — matches ReservationViewSet.cancel.
            outbound = obj.delivery_orders.filter(direction='outbound').order_by('-assigned_at').first()
            if outbound and outbound.status != 'assigned':
                return False
            return True
        return False

    def get_can_approve(self, obj):
        """Only the buyer may confirm receipt and release payment, and only
        while there's still something to release."""
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user is None or obj.buyer_id != user.id:
            return False
        outbound = obj.delivery_orders.filter(direction='outbound').order_by('-assigned_at').first()
        if outbound and outbound.status != 'confirmed':
            # Once a delivery company is handling this sale, the buyer must
            # first confirm physical receipt via the delivery order itself
            # (DeliveryOrderViewSet.confirm_receipt) before payment release
            # becomes an available choice — releasing money before the buyer
            # has even confirmed they hold the item defeats the escrow.
            return False
        return (
            obj.status == 'completed'
            and not obj.buyer_approved
            and not obj.payment_released_to_seller
        )

    def get_delivery_order(self, obj):
        """The most recent delivery leg (outbound or a return) for this
        reservation, if the seller ever sent it to the delivery company."""
        delivery_order = obj.delivery_orders.order_by('-assigned_at').first()
        if not delivery_order:
            return None
        return DeliveryOrderSerializer(delivery_order).data

    def get_can_send_to_delivery(self, obj):
        """Mirrors ReservationViewSet.send_to_delivery's eligibility rules —
        seller-only, while paid and not already handed off, and only if
        there's actually an active delivery company to hand it to."""
        from .delivery_utils import get_active_delivery_company

        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user is None:
            return False
        is_seller = obj.item.owner_id == user.id or obj.item.current_holder_id == user.id
        if not is_seller or obj.status != 'completed' or not obj.delivery_location_id:
            return False
        if not get_active_delivery_company():
            return False
        return not obj.delivery_orders.filter(direction='outbound').exists()

    def get_can_request_payment_release(self, obj):
        """
        Mirrors ReservationViewSet.request_payment_release's eligibility
        rules — seller-only, while payment is still held. This is just a
        notification nudge, not a money movement (the buyer still has to
        explicitly approve, or 7 days have to pass) — can_approve is what
        actually gates the money.

        Visibility still tracks the delivery hand-off though: once a
        delivery company is in the picture, nudging the buyer before the
        seller has even sent the item off is premature — the seller's own
        fulfilment step is send_to_delivery, not payment itself. So this
        stays hidden until an outbound DeliveryOrder exists, unless there's
        no active delivery company to send it to in the first place (the
        old direct hand-off flow, where paying is the only fulfilment step).
        """
        from .delivery_utils import get_active_delivery_company

        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        if user is None:
            return False
        is_seller = obj.item.owner_id == user.id or (
            obj.item.current_holder_id == user.id and obj.is_transfer_resale
        )
        if not is_seller or obj.status != 'completed' or obj.payment_released_to_seller:
            return False
        if obj.delivery_orders.filter(direction='outbound').exists():
            return True
        return get_active_delivery_company() is None

    def get_payment_status(self, obj):
        if obj.payment_released_to_seller:
            return {
                'status': 'released',
                'released_at': obj.payment_released_at,
                'message': 'Payment has been released to seller'
            }
        elif obj.buyer_approved:
            return {
                'status': 'approved_pending_release',
                'approved_at': obj.buyer_approved_at,
                'message': 'Payment approved, pending release'
            }
        elif obj.status == 'completed' and obj.completed_at:
            days_remaining = 7 - (timezone.now() - obj.completed_at).days
            return {
                'status': 'held',
                'days_remaining': max(0, days_remaining),
                'message': f'Payment held. Auto-release in {max(0, days_remaining)} days or approve to release immediately'
            }
        elif obj.status == 'refunded':
            return {
                'status': 'refunded',
                'message': 'Payment has been refunded to buyer'
            }
        else:
            return {
                'status': 'pending',
                'message': 'Payment not yet processed'
            }


class CreateReservationSerializer(serializers.Serializer):
    """Serializer for creating a reservation"""
    item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(default=1, min_value=1)
    delivery_location_id = serializers.IntegerField(required=False, allow_null=True)
    coupon_collection_id = serializers.IntegerField(required=False, allow_null=True)
    offer_message_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_item_id(self, value):
        try:
            item = Item.objects.get(id=value)
            if item.status not in ['active', 'reserved']:
                raise serializers.ValidationError("Item is not available for reservation")
            return value
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

    def validate_quantity(self, value):
        if value < 1:
            raise serializers.ValidationError("Quantity must be at least 1")
        return value

    def validate_delivery_location_id(self, value):
        if value:
            request = self.context.get('request')
            try:
                Location.objects.get(id=value, user=request.user)
                return value
            except Location.DoesNotExist:
                raise serializers.ValidationError("Delivery location not found or doesn't belong to you")
        return value


class DisputeSerializer(serializers.ModelSerializer):
    """Dispute serializer"""
    item = ItemSerializer(read_only=True)
    reservation = ReservationSerializer(read_only=True)
    complainant = UserSerializer(read_only=True)
    defendant = UserSerializer(read_only=True)
    can_resolve = serializers.SerializerMethodField()

    class Meta:
        model = Dispute
        fields = [
            'id', 'item', 'reservation', 'complainant', 'defendant',
            'title', 'description', 'reason', 'status', 'admin_notes',
            'resolution', 'resolution_action', 'created_at', 'resolved_at',
            'can_resolve'
        ]
        read_only_fields = ['created_at', 'resolved_at']

    def get_can_resolve(self, obj):
        return obj.status in ['open', 'investigating']


from .models import DeliveryOrder


class DeliveryOrderSerializer(serializers.ModelSerializer):
    """
    Delivery order serializer. Intentionally exposes only what the company
    needs to fulfil the leg (contact/address, item summary, cash to
    collect) — no buyer/seller wallet or pricing data.
    """
    reservation = serializers.PrimaryKeyRelatedField(read_only=True)
    batch = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = DeliveryOrder
        fields = [
            'id', 'reservation', 'batch', 'direction', 'status',
            'from_name', 'from_phone', 'from_address', 'from_lat', 'from_lng',
            'to_name', 'to_phone', 'to_address', 'to_lat', 'to_lng',
            'item_summary', 'quantity', 'special_notes', 'cash_to_collect',
            'cash_collected', 'cash_collected_at', 'delivery_fee',
            'failure_reason',
            'assigned_at', 'picked_up_at', 'delivered_at', 'confirmed_at', 'refused_at',
        ]
        read_only_fields = fields


class CreateDisputeSerializer(serializers.Serializer):
    """Serializer for creating a dispute"""
    item_id = serializers.IntegerField()
    reservation_id = serializers.IntegerField(required=False, allow_null=True)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField()
    reason = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate_item_id(self, value):
        try:
            Item.objects.get(id=value)
            return value
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

    def validate_reservation_id(self, value):
        if value is not None:
            try:
                Reservation.objects.get(id=value)
                return value
            except Reservation.DoesNotExist:
                raise serializers.ValidationError("Reservation not found")
        return value

    def validate(self, data):
        item_id = data.get('item_id')
        reservation_id = data.get('reservation_id')

        if reservation_id:
            try:
                reservation = Reservation.objects.get(id=reservation_id)
                if reservation.item.id != item_id:
                    raise serializers.ValidationError(
                        "Reservation does not belong to the specified item"
                    )
            except Reservation.DoesNotExist:
                raise serializers.ValidationError("Reservation not found")

        return data


class ResolveDisputeSerializer(serializers.Serializer):
    """Serializer for resolving a dispute"""
    resolution = serializers.CharField()
    resolution_action = serializers.ChoiceField(
        choices=[
            ('refund_buyer', 'Refund to Buyer'),
            ('pay_seller', 'Pay Seller'),
            ('partial_refund', 'Partial Refund'),
            ('no_action', 'No Action'),
        ]
    )
    admin_notes = serializers.CharField(required=False, allow_blank=True)
    partial_refund_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True
    )

    def validate(self, data):
        if data.get('resolution_action') == 'partial_refund':
            if not data.get('partial_refund_amount'):
                raise serializers.ValidationError(
                    "partial_refund_amount is required when resolution_action is 'partial_refund'"
                )
            if data['partial_refund_amount'] <= 0:
                raise serializers.ValidationError(
                    "partial_refund_amount must be greater than 0"
                )
        return data


class DisputeUpdateSerializer(serializers.Serializer):
    """Serializer for updating dispute status"""
    status = serializers.ChoiceField(
        choices=[
            ('open', 'Open'),
            ('investigating', 'Investigating'),
            ('resolved', 'Resolved'),
            ('closed', 'Closed'),
        ]
    )
    admin_notes = serializers.CharField(required=False, allow_blank=True)


class SellerReservationSerializer(serializers.ModelSerializer):
    """Serializer for sellers to view their item reservations"""
    item = ItemSerializer(read_only=True)
    buyer = UserSerializer(read_only=True)
    delivery_location = LocationSerializer(read_only=True)
    time_remaining = serializers.SerializerMethodField()
    can_approve_cancellation = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            'id', 'item', 'buyer', 'quantity', 'deposit_amount', 'total_price',
            'status', 'reserved_at', 'expires_at', 'completed_at',
            'seller_cancellation_approved', 'payment_released_to_seller',
            'payment_released_at', 'delivery_location',
            'time_remaining', 'can_approve_cancellation'
        ]

    def get_time_remaining(self, obj):
        if obj.status == 'pending':
            remaining = obj.expires_at - timezone.now()
            return int(remaining.total_seconds()) if remaining.total_seconds() > 0 else 0
        return 0

    def get_can_approve_cancellation(self, obj):
        return obj.status == 'pending' and not obj.seller_cancellation_approved


class RepresentativeSerializer(serializers.ModelSerializer):
    """Full representative serializer"""
    item = ItemSerializer(read_only=True)
    owner = UserSerializer(read_only=True)
    representative = UserSerializer(read_only=True)
    can_accept = serializers.SerializerMethodField()
    can_reject = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = Representative
        fields = [
            'id', 'item', 'owner', 'representative',
            'commission_percentage', 'status',
            'can_list', 'can_negotiate', 'can_complete_sale',
            'request_message', 'rejection_reason',
            'created_at', 'accepted_at', 'rejected_at',
            'completed_at', 'cancelled_at',
            'can_accept', 'can_reject', 'can_cancel'
        ]
        read_only_fields = [
            'created_at', 'accepted_at', 'rejected_at',
            'completed_at', 'cancelled_at'
        ]

    def get_can_accept(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        return obj.owner == request.user and obj.status == 'pending'

    def get_can_reject(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        return obj.owner == request.user and obj.status == 'pending'

    def get_can_cancel(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        return (
                (obj.representative == request.user and obj.status == 'pending') or
                (obj.owner == request.user and obj.status in ['accepted', 'active'])
        )


class RepresentativeCreateSerializer(serializers.Serializer):
    """Serializer for creating a representative request"""
    item_id = serializers.IntegerField()
    commission_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100
    )
    can_list = serializers.BooleanField(default=True)
    can_negotiate = serializers.BooleanField(default=True)
    can_complete_sale = serializers.BooleanField(default=True)
    request_message = serializers.CharField(required=False, allow_blank=True)

    def validate_item_id(self, value):
        try:
            item = Item.objects.get(id=value)
            if item.status not in ['active', 'reserved']:
                raise serializers.ValidationError("Item is not available for representation")
            return value
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

    def validate(self, data):
        item_id = data.get('item_id')
        request = self.context.get('request')

        if not request:
            raise serializers.ValidationError("Request context required")

        try:
            item = Item.objects.get(id=item_id)

            if item.owner == request.user:
                raise serializers.ValidationError("You cannot represent your own item")

            existing = Representative.objects.filter(
                item=item,
                representative=request.user,
                status='pending'
            ).exists()

            if existing:
                raise serializers.ValidationError(
                    "You already have a pending request for this item"
                )

            active = Representative.objects.filter(
                item=item,
                representative=request.user,
                status__in=['accepted', 'active']
            ).exists()

            if active:
                raise serializers.ValidationError(
                    "You are already representing this item"
                )

        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

        return data


class RepresentativeAcceptSerializer(serializers.Serializer):
    """Serializer for accepting a representative request"""
    pass


class RepresentativeRejectSerializer(serializers.Serializer):
    """Serializer for rejecting a representative request"""
    rejection_reason = serializers.CharField(required=False, allow_blank=True)


class RepresentativeUpdatePermissionsSerializer(serializers.Serializer):
    """Serializer for updating representative permissions"""
    can_list = serializers.BooleanField(required=False)
    can_negotiate = serializers.BooleanField(required=False)
    can_complete_sale = serializers.BooleanField(required=False)
    commission_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        min_value=0,
        max_value=100,
        required=False
    )


class ItemTransferSerializer(serializers.ModelSerializer):
    """Wholesale ownership transfer — full detail view"""
    item = ItemListSerializer(read_only=True)
    owner = UserSerializer(source='from_user', read_only=True)
    reseller = UserSerializer(source='to_user', read_only=True)
    resulting_item = ItemListSerializer(read_only=True)
    storage_fee_payments = StorageFeePaymentSerializer(many=True, read_only=True)
    days_remaining = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    total_wholesale_price = serializers.SerializerMethodField()
    due_storage_fee = serializers.SerializerMethodField()
    is_storage_fee_due = serializers.SerializerMethodField()
    # Permissions for the requesting user
    can_approve = serializers.SerializerMethodField()
    can_reject = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = ItemTransfer
        fields = [
            'id', 'item', 'owner', 'reseller', 'status',
            'request_message', 'rejection_reason', 'notes',
            # Agreement duration
            'duration_days', 'agreement_start_date', 'agreement_end_date',
            'days_remaining', 'is_expired',
            # Wholesale purchase
            'quantity', 'wholesale_price_per_unit', 'total_wholesale_price', 'resulting_item',
            # Storage fees
            'storage_fee_type', 'storage_fee_amount', 'storage_fee_period_days',
            'total_storage_fees_paid', 'next_storage_fee_due',
            'due_storage_fee', 'is_storage_fee_due', 'storage_fee_payments',
            # Timestamps
            'initiated_at', 'approved_at', 'rejected_at', 'completed_at', 'cancelled_at',
            # Permissions
            'can_approve', 'can_reject', 'can_cancel',
        ]

    def get_days_remaining(self, obj):
        return obj.days_remaining()

    def get_is_expired(self, obj):
        return obj.is_expired()

    def get_total_wholesale_price(self, obj):
        total = obj.total_wholesale_price
        return float(total) if total is not None else None

    def get_due_storage_fee(self, obj):
        return float(obj.calculate_due_storage_fee())

    def get_is_storage_fee_due(self, obj):
        return obj.is_storage_fee_due()

    def get_can_approve(self, obj):
        req = self.context.get('request')
        return req and obj.from_user == req.user and obj.status == 'pending'

    def get_can_reject(self, obj):
        req = self.context.get('request')
        return req and obj.from_user == req.user and obj.status == 'pending'

    def get_can_cancel(self, obj):
        req = self.context.get('request')
        if not req:
            return False
        return (
            (obj.to_user == req.user and obj.status == 'pending') or
            (obj.from_user == req.user and obj.status == 'active')
        )


class ResaleAgreementCreateSerializer(serializers.Serializer):
    """Reseller requests a quantity of the owner's item for wholesale ownership transfer"""
    item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, default=1)
    storage_fee_type = serializers.ChoiceField(
        choices=ItemTransfer.STORAGE_FEE_TYPE_CHOICES, default='none'
    )
    storage_fee_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, min_value=0
    )
    storage_fee_period_days = serializers.IntegerField(default=30, min_value=1, max_value=365)
    duration_days = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=730)
    request_message = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_item_id(self, value):
        try:
            item = Item.objects.get(id=value)
            if item.status not in ['active', 'reserved']:
                raise serializers.ValidationError("Item is not available")
            fixed_listing = getattr(item, 'fixed_listing', None)
            if not fixed_listing or not fixed_listing.price or fixed_listing.price <= 0:
                raise serializers.ValidationError("Item has no fixed price listing")
            return value
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

    def validate(self, data):
        request = self.context.get('request')
        item = Item.objects.get(id=data['item_id'])
        if item.owner == request.user:
            raise serializers.ValidationError("You cannot request your own item")
        if data['quantity'] > item.quantity:
            raise serializers.ValidationError("Requested quantity exceeds available stock")
        already_active = ItemTransfer.objects.filter(
            item=item, to_user=request.user, status__in=['pending', 'active']
        ).exists()
        if already_active:
            raise serializers.ValidationError("You already have an active agreement for this item")
        if data['storage_fee_type'] != 'none' and not data.get('storage_fee_amount'):
            raise serializers.ValidationError("storage_fee_amount is required when storage_fee_type is set")
        return data


class ResaleRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField(required=False, allow_blank=True)


class TransactionSerializer(serializers.ModelSerializer):
    """Transaction serializer - ENHANCED"""
    user = UserSerializer(read_only=True)
    item = ItemSerializer(read_only=True)
    transfer = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Transaction
        fields = [
            'id', 'user', 'transaction_type', 'amount', 'status',
            'item', 'reservation', 'auction', 'transfer',
            'stripe_payment_intent_id',
            'created_at', 'completed_at'
        ]


class MessageSerializer(serializers.ModelSerializer):
    """Chat message serializer"""
    sender = UserSerializer(read_only=True)
    sender_name = serializers.SerializerMethodField()
    attachment_url = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()
    is_redeemed = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'chat', 'sender', 'sender_name', 'message_type',
            'content', 'offer_amount', 'offer_quantity', 'offer_status', 'attachment_url',
            'is_read', 'is_deleted_by_sender', 'is_deleted_by_receiver',
            'created_at', 'edited_at', 'can_edit', 'can_delete', 'is_redeemed'
        ]
        read_only_fields = ['sender', 'message_type', 'created_at', 'edited_at']

    def get_is_redeemed(self, obj):
        return hasattr(obj, 'redeemed_in_reservation')

    def get_sender_name(self, obj):
        return obj.sender.username if obj.sender else "System"

    def get_attachment_url(self, obj):
        if not obj.attachment:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.attachment.url) if request else obj.attachment.url

    def get_can_edit(self, obj):
        request = self.context.get('request')
        if not request or not obj.sender:
            return False
        time_limit = timezone.now() - timezone.timedelta(minutes=15)
        return (
                obj.sender == request.user and
                obj.message_type == 'user' and
                obj.created_at > time_limit
        )

    def get_can_delete(self, obj):
        request = self.context.get('request')
        if not request or not obj.sender:
            return False
        return obj.sender == request.user


class MessageCreateSerializer(serializers.Serializer):
    """Serializer for creating messages"""
    content = serializers.CharField(required=False, allow_blank=True, default='')
    attachment = serializers.FileField(required=False, allow_null=True)

    def validate_attachment(self, value):
        if not value:
            return value
        if not (value.content_type or '').startswith('image/'):
            raise serializers.ValidationError("Attachment must be an image.")
        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Image must be 5MB or smaller.")
        return value

    def validate(self, data):
        if not data.get('content', '').strip() and not data.get('attachment'):
            raise serializers.ValidationError("Message must have content or an attachment.")
        return data


class MessageOfferSerializer(serializers.Serializer):
    """Serializer for sending price offers"""
    offer_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    quantity = serializers.IntegerField(default=1, min_value=1)
    message = serializers.CharField(required=False, allow_blank=True)


class ChatSerializer(serializers.ModelSerializer):
    """Chat serializer"""
    item = ItemSerializer(read_only=True)
    buyer = UserSerializer(read_only=True)
    seller = UserSerializer(read_only=True)
    store = serializers.SerializerMethodField()
    latest_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    other_user = serializers.SerializerMethodField()
    is_archived = serializers.SerializerMethodField()
    is_support = serializers.SerializerMethodField()
    is_own_store = serializers.SerializerMethodField()

    class Meta:
        model = Chat
        fields = [
            'id', 'item', 'store', 'buyer', 'seller', 'other_user',
            'latest_message', 'unread_count', 'is_active', 'is_archived',
            'is_support', 'is_own_store', 'created_at', 'updated_at'
        ]

    def get_store(self, obj):
        if not obj.store:
            return None
        request = self.context.get('request')
        logo_url = None
        if obj.store.logo:
            logo_url = request.build_absolute_uri(obj.store.logo.url) if request else obj.store.logo.url
        return {'id': obj.store.id, 'name': obj.store.name, 'logo_url': logo_url}

    def get_latest_message(self, obj):
        latest = obj.messages.filter(
            is_deleted_by_sender=False,
            is_deleted_by_receiver=False
        ).last()
        if latest:
            return {
                'id': latest.id,
                'content': latest.content,
                'type': latest.message_type,
                'sender': latest.sender.username if latest.sender else 'System',
                'created_at': latest.created_at,
                'is_read': latest.is_read
            }
        return None

    def get_unread_count(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return 0
        return obj.get_unread_count(request.user)

    def get_other_user(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return None
        other = obj.get_other_user(request.user)
        return UserSerializer(other).data

    def get_is_archived(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return False
        if request.user == obj.buyer:
            return obj.is_archived_by_buyer
        elif request.user == obj.seller:
            return obj.is_archived_by_seller
        return False

    def get_is_own_store(self, obj):
        # `store` above is always the contacted store's own info regardless
        # of who's asking — a store-level chat's `seller` is always that
        # store's owner (see ChatViewSet.create_or_get_store), so this tells
        # the client whether *this* viewer IS that owner. Without it, the
        # store owner's inbox would show their own store's name/logo as the
        # conversation's identity instead of the customer's.
        request = self.context.get('request')
        if not request or not request.user or not obj.store:
            return False
        return request.user == obj.seller

    def get_is_support(self, obj):
        # The fixed support account is always the seller (see
        # core/services/support.py:start_support_chat) and a support chat
        # has neither an item nor a store attached.
        return (
            obj.item_id is None and obj.store_id is None
            and obj.seller.username == SUPPORT_USERNAME
        )


# A long-running chat (e.g. a store relationship spanning months) can grow
# an unbounded message history — ChatDetailSerializer only ships the latest
# page inline; ChatViewSet.messages fetches earlier pages as the user
# scrolls up. Shared so both stay in lockstep on what "a page" means.
MESSAGES_PAGE_SIZE = 30


class ChatDetailSerializer(serializers.ModelSerializer):
    """Detailed chat serializer with messages"""
    item = ItemSerializer(read_only=True)
    buyer = UserSerializer(read_only=True)
    seller = UserSerializer(read_only=True)
    store = serializers.SerializerMethodField()
    messages = serializers.SerializerMethodField()
    has_more_messages = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    other_user = serializers.SerializerMethodField()
    is_support = serializers.SerializerMethodField()
    is_own_store = serializers.SerializerMethodField()

    class Meta:
        model = Chat
        fields = [
            'id', 'item', 'store', 'buyer', 'seller', 'other_user',
            'messages', 'has_more_messages', 'unread_count', 'is_active',
            'is_support', 'is_own_store', 'created_at', 'updated_at'
        ]

    # مكرّرة عمداً من ChatSerializer (نفس نمط other_user/unread_count أدناه) —
    # الشاشة التي تفتح محادثة عبر chatId فقط (مثلاً من إشعار push) تحتاج هذه
    # الحقول أيضاً لتحديد عنوان الشاشة بشكل صحيح دون تمريره من القائمة أولاً.
    def get_store(self, obj):
        if not obj.store:
            return None
        request = self.context.get('request')
        logo_url = None
        if obj.store.logo:
            logo_url = request.build_absolute_uri(obj.store.logo.url) if request else obj.store.logo.url
        return {'id': obj.store.id, 'name': obj.store.name, 'logo_url': logo_url}

    def get_is_support(self, obj):
        return (
            obj.item_id is None and obj.store_id is None
            and obj.seller.username == SUPPORT_USERNAME
        )

    def get_is_own_store(self, obj):
        request = self.context.get('request')
        if not request or not request.user or not obj.store:
            return False
        return request.user == obj.seller

    def _visible_messages(self, obj):
        request = self.context.get('request')
        messages = obj.messages.all()

        if request and request.user:
            if request.user == obj.buyer:
                messages = messages.filter(is_deleted_by_receiver=False)
            elif request.user == obj.seller:
                messages = messages.exclude(
                    sender=obj.buyer,
                    is_deleted_by_receiver=True
                )
            # A message the viewer sent and then deleted (destroy() sets
            # is_deleted_by_sender) must stay hidden from their own view.
            messages = messages.exclude(sender=request.user, is_deleted_by_sender=True)

        return messages

    def get_messages(self, obj):
        latest = list(
            self._visible_messages(obj).order_by('-created_at')[:MESSAGES_PAGE_SIZE]
        )
        latest.reverse()  # صعوداً زمنياً، كما كانت قبل تفعيل الترقيم
        return MessageSerializer(latest, many=True, context=self.context).data

    def get_has_more_messages(self, obj):
        return self._visible_messages(obj).count() > MESSAGES_PAGE_SIZE

    def get_unread_count(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return 0
        return obj.get_unread_count(request.user)

    def get_other_user(self, obj):
        request = self.context.get('request')
        if not request or not request.user:
            return None
        other = obj.get_other_user(request.user)
        return UserSerializer(other).data


class ChatCreateSerializer(serializers.Serializer):
    """Serializer for creating/getting a chat"""
    item_id = serializers.IntegerField()
    initial_message = serializers.CharField(required=False, allow_blank=True)

    def validate_item_id(self, value):
        try:
            Item.objects.get(id=value)
            return value
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

    def validate(self, data):
        request = self.context.get('request')
        item_id = data.get('item_id')

        try:
            item = Item.objects.get(id=item_id)

            if item.owner == request.user:
                raise serializers.ValidationError("You cannot chat about your own item")

        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")

        return data


class RatingSerializer(serializers.ModelSerializer):
    """Rating serializer"""
    rater = UserSerializer(read_only=True)
    rated_user = UserSerializer(read_only=True)
    item = ItemSerializer(read_only=True)

    class Meta:
        model = Rating
        fields = ['id', 'rater', 'rated_user', 'rating_type', 'score',
                  'comment', 'item', 'created_at']
        read_only_fields = ['rater']


class RatingCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating ratings"""
    rated_user_id = serializers.IntegerField(write_only=True)
    item_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = Rating
        fields = ['rated_user_id', 'item_id', 'rating_type', 'score', 'comment']

    def validate_score(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Score must be between 1 and 5")
        return value

    def validate(self, data):
        try:
            rated_user = User.objects.get(id=data['rated_user_id'])

            request = self.context.get('request')
            if rated_user == request.user:
                raise serializers.ValidationError("You cannot rate yourself")

            return data
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found")

    def create(self, validated_data):
        rated_user_id = validated_data.pop('rated_user_id')
        item_id = validated_data.pop('item_id', None)

        rated_user = User.objects.get(id=rated_user_id)
        item = Item.objects.get(id=item_id) if item_id else None

        return Rating.objects.create(
            rated_user=rated_user,
            item=item,
            **validated_data
        )


class UserRegistrationSerializer(serializers.ModelSerializer):
    """User registration serializer"""
    password = serializers.CharField(write_only=True, min_length=8)
    password2 = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ['username', 'email', 'phone', 'password', 'password2']

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError("Passwords don't match")
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        user = User.objects.create_user(**validated_data)
        return user


class UserLoginSerializer(serializers.Serializer):
    """User login serializer"""
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


class LogoutSerializer(serializers.Serializer):
    refresh_token = serializers.CharField(
        help_text="The refresh token to blacklist",
        required=True
    )


class ChangePasswordSerializer(serializers.Serializer):
    """Change password serializer"""
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password2 = serializers.CharField(write_only=True, min_length=8)

    def validate(self, data):
        if data['new_password'] != data['new_password2']:
            raise serializers.ValidationError("New passwords don't match")
        return data


class ResetPasswordSerializer(serializers.Serializer):
    """Reset password serializer"""
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password2 = serializers.CharField(write_only=True, min_length=8)

    def validate(self, data):
        if data['new_password'] != data['new_password2']:
            raise serializers.ValidationError("Passwords don't match")
        return data


class UserStoreCreateSerializer(serializers.Serializer):
    """Serializer for creating a user store"""
    name = serializers.CharField(
        max_length=200,
        help_text="Your store name (e.g., 'John's Electronics')"
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Brief description of your store"
    )
    logo = serializers.ImageField(
        required=False,
        allow_null=True,
        help_text="Store logo image"
    )
    cover_image = serializers.ImageField(
        required=False,
        allow_null=True,
        help_text="Store cover/banner image"
    )
    default_location_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Default location for items in this store"
    )
    phone_number = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        help_text="Store contact phone number"
    )
    whatsapp_number = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        help_text="Store WhatsApp number"
    )

    def validate_name(self, value):
        """Ensure store name is not empty"""
        if not value.strip():
            raise serializers.ValidationError("Store name cannot be empty")
        return value.strip()


class UserStoreUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating store details"""
    default_location_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Update default location"
    )

    class Meta:
        model = UserStore
        fields = [
            'name', 'description', 'logo', 'cover_image', 'default_location_id',
            'is_active', 'phone_number', 'whatsapp_number',
        ]
        extra_kwargs = {
            'name': {'required': False},
            'description': {'required': False},
            'logo': {'required': False},
            'cover_image': {'required': False},
            'is_active': {'required': False},
            'phone_number': {'required': False},
            'whatsapp_number': {'required': False},
        }

    def validate_name(self, value):
        """Ensure store name is not empty"""
        if value and not value.strip():
            raise serializers.ValidationError("Store name cannot be empty")
        return value.strip() if value else value

    def validate_default_location_id(self, value):
        """Validate location belongs to user"""
        if value:
            request = self.context.get('request')
            try:
                Location.objects.get(id=value, user=request.user)
                return value
            except Location.DoesNotExist:
                raise serializers.ValidationError(
                    "Location not found or doesn't belong to you"
                )
        return value

    def update(self, instance, validated_data):
        """Handle location update separately"""
        location_id = validated_data.pop('default_location_id', None)

        if location_id is not None:
            if location_id:
                location = Location.objects.get(id=location_id)
                instance.default_location = location
            else:
                instance.default_location = None

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class UserStoreSerializer(serializers.ModelSerializer):
    """Full UserStore serializer with all details"""
    user = UserSerializer(read_only=True)
    default_location = LocationSerializer(read_only=True)
    total_items = serializers.SerializerMethodField()
    total_active_items = serializers.SerializerMethodField()
    logo_url = serializers.SerializerMethodField()
    cover_image_url = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    total_reviews = serializers.SerializerMethodField()
    # --- follow / storefront stats ---
    is_following = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    positive_review_percent = serializers.SerializerMethodField()
    communication_rating = serializers.SerializerMethodField()
    total_sold = serializers.SerializerMethodField()

    class Meta:
        model = UserStore
        fields = [
            'id', 'user', 'name', 'description', 'logo', 'logo_url',
            'cover_image', 'cover_image_url',
            'is_active', 'default_location', 'created_at',
            'total_items', 'total_active_items', 'distance_km',
            'rating', 'total_reviews',
            'is_following', 'followers_count', 'positive_review_percent',
            'communication_rating', 'total_sold',
            'phone_number', 'whatsapp_number',
        ]
        read_only_fields = ['user', 'created_at', 'rating']

    def get_cover_image_url(self, obj):
        if obj.cover_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.cover_image.url)
            return obj.cover_image.url
        return None

    def get_distance_km(self, obj):
        return getattr(obj, 'distance_km', None)

    def get_total_items(self, obj):
        """Count total items owned by this user"""
        return Item.objects.filter(owner=obj.user).count()

    def get_total_active_items(self, obj):
        """Count active items (including those being resold)"""
        from django.db.models import Q
        return Item.objects.filter(
            Q(owner=obj.user) | Q(current_holder=obj.user)
        ).filter(status__in=['active', 'reserved']).count()

    def get_logo_url(self, obj):
        """Get full URL for logo"""
        if obj.logo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.logo.url)
            return obj.logo.url
        return None

    def get_total_reviews(self, obj):
        return obj.store_reviews.count()

    def get_is_following(self, obj):
        """Whether the current user follows this store"""
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.followers.filter(follower=request.user).exists()

    def get_followers_count(self, obj):
        return obj.followers.count()

    def get_positive_review_percent(self, obj):
        """Percent of store reviews rated 4 stars or higher"""
        reviews = obj.store_reviews.all()
        total = reviews.count()
        if not total:
            return 0.0
        positive = reviews.filter(score__gte=4).count()
        return round(positive * 100.0 / total, 1)

    def get_communication_rating(self, obj):
        """Communication score, derived from the store owner's trust score (0-5)"""
        return float(obj.user.trust_score or 0)

    def get_total_sold(self, obj):
        """Total number of items the store owner has sold"""
        return obj.user.total_sales


class PublicUserStoreSerializer(serializers.ModelSerializer):
    """Public-facing store serializer (limited info)"""
    owner_username = serializers.CharField(source='user.username', read_only=True)
    owner_trust_score = serializers.DecimalField(
        source='user.trust_score',
        read_only=True,
        max_digits=3,
        decimal_places=2
    )
    logo_url = serializers.SerializerMethodField()
    total_active_items = serializers.SerializerMethodField()

    class Meta:
        model = UserStore
        fields = [
            'id', 'owner_username', 'owner_trust_score', 'name',
            'description', 'logo_url', 'created_at', 'total_active_items',
            'phone_number', 'whatsapp_number',
        ]

    def get_logo_url(self, obj):
        """Get full URL for logo"""
        if obj.logo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.logo.url)
            return obj.logo.url
        return None

    def get_total_active_items(self, obj):
        """Count active items in store"""
        from django.db.models import Q
        return Item.objects.filter(
            Q(owner=obj.user) | Q(current_holder=obj.user)
        ).filter(status__in=['active', 'reserved']).count()


class StoreCouponSerializer(serializers.ModelSerializer):
    """Coupon a store offers to customers"""
    is_collected = serializers.SerializerMethodField()
    store_name = serializers.CharField(source='store.name', read_only=True)

    class Meta:
        model = StoreCoupon
        fields = [
            'id', 'store', 'store_name', 'code', 'amount', 'min_order_amount',
            'description', 'is_active', 'expires_at', 'created_at',
            'is_collected',
        ]
        read_only_fields = ['store', 'created_at']

    def get_is_collected(self, obj):
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.collections.filter(user=request.user).exists()


class CouponCollectionSerializer(serializers.ModelSerializer):
    """A coupon the current user has collected (كوبوناتي - محفظة الزبون)"""
    coupon = StoreCouponSerializer(read_only=True)
    is_used = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = CouponCollection
        fields = [
            'id', 'coupon', 'collected_at', 'is_used', 'is_expired',
        ]

    def get_is_used(self, obj):
        return hasattr(obj, 'used_in_reservation')

    def get_is_expired(self, obj):
        return not obj.coupon.is_valid


class LiveSessionItemSerializer(serializers.ModelSerializer):
    """A queued/shown product during a live session, with its live-only price/deposit"""
    item = ItemListSerializer(read_only=True)

    class Meta:
        model = LiveSessionItem
        fields = ['id', 'item', 'price', 'deposit_amount', 'order', 'status', 'shown_at', 'created_at']
        read_only_fields = ['status', 'shown_at', 'created_at']


class LiveSessionItemCreateSerializer(serializers.Serializer):
    """Add a product to a live session's queue with a live-only price/deposit"""
    item_id = serializers.IntegerField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    deposit_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))
    order = serializers.IntegerField(required=False, default=0)

    def validate_item_id(self, value):
        try:
            Item.objects.get(id=value)
        except Item.DoesNotExist:
            raise serializers.ValidationError("Item not found")
        return value

    def validate(self, data):
        if data['deposit_amount'] > data['price']:
            raise serializers.ValidationError("Deposit amount cannot exceed the price")
        return data


class LiveSessionCardSerializer(serializers.ModelSerializer):
    """Lightweight card for the live-shopping discovery/browse feed"""
    store = PublicUserStoreSerializer(read_only=True)
    thumbnail = serializers.SerializerMethodField()
    is_liked_by_me = serializers.SerializerMethodField()

    class Meta:
        model = LiveSession
        fields = [
            'id', 'store', 'title', 'thumbnail', 'status',
            'current_viewer_count', 'peak_viewer_count', 'total_views', 'likes_count',
            'is_liked_by_me', 'scheduled_at', 'started_at', 'created_at',
        ]

    def get_thumbnail(self, obj):
        if not obj.thumbnail:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.thumbnail.url) if request else obj.thumbnail.url

    def get_is_liked_by_me(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return obj.likes.filter(user=request.user).exists()


class LiveSessionSerializer(LiveSessionCardSerializer):
    """Full live session detail — includes the queue and the current now_showing item.
    Deliberately never exposes agora_channel_name (only join_token does)."""
    queue_items = LiveSessionItemSerializer(source='items', many=True, read_only=True)
    current_now_showing = serializers.SerializerMethodField()
    current_live_auction = serializers.SerializerMethodField()

    class Meta(LiveSessionCardSerializer.Meta):
        fields = LiveSessionCardSerializer.Meta.fields + [
            'description', 'ended_at', 'queue_items', 'current_now_showing', 'current_live_auction',
        ]

    def get_current_now_showing(self, obj):
        now_showing = obj.items.filter(status='now_showing').first()
        return LiveSessionItemSerializer(now_showing, context=self.context).data if now_showing else None

    def get_current_live_auction(self, obj):
        auction = obj.auctions.filter(status='active').first()
        return LiveAuctionSerializer(auction, context=self.context).data if auction else None


class LiveSessionCreateSerializer(serializers.Serializer):
    """Validates input for starting/scheduling a new live session"""
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    thumbnail = serializers.ImageField(required=False, allow_null=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    start_immediately = serializers.BooleanField(default=False)


class ReserveLiveItemSerializer(serializers.Serializer):
    """Validates input for the one-tap 'reserve' action during a live session"""
    queue_item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(default=1, min_value=1)
    screenshot = serializers.ImageField()
    delivery_location_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_delivery_location_id(self, value):
        if value:
            request = self.context.get('request')
            try:
                Location.objects.get(id=value, user=request.user)
                return value
            except Location.DoesNotExist:
                raise serializers.ValidationError("Delivery location not found or doesn't belong to you")
        return value


class LiveSessionCommentSerializer(serializers.ModelSerializer):
    """Public live comment. Deliberately exposes only id/username — never
    nests the full UserSerializer here, since comments are visible to every
    (possibly anonymous) viewer and UserSerializer includes email/phone."""
    username = serializers.CharField(source='user.username', read_only=True)
    user_id = serializers.IntegerField(source='user.id', read_only=True)

    class Meta:
        model = LiveSessionComment
        fields = ['id', 'user_id', 'username', 'content', 'created_at']


class LiveSessionCommentCreateSerializer(serializers.Serializer):
    content = serializers.CharField(max_length=300, trim_whitespace=True)

    def validate_content(self, value):
        if not value.strip():
            raise serializers.ValidationError("Comment cannot be empty")
        return value.strip()


class ShowLiveItemSerializer(serializers.Serializer):
    """One-tap 'show this product now' for the store owner. price/deposit_amount
    are optional overrides — if omitted, the item's existing FixedPriceListing
    price/deposit are used automatically so nothing needs to be typed for an
    item that's already listed for sale."""
    item_id = serializers.IntegerField()
    price = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal('0.01'),
        required=False, allow_null=True
    )
    deposit_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal('0.00'),
        required=False, allow_null=True
    )


class QuickShowItemSerializer(serializers.Serializer):
    """Show a product that was never added to the catalog before — the
    seller just holds it up on camera and types a title + price + deposit
    right here; a minimal backing Item is created transparently so the
    seller never has to visit a separate 'add product' screen first."""
    title = serializers.CharField(max_length=200)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    deposit_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))
    quantity = serializers.IntegerField(default=1, min_value=1)
    category_id = serializers.IntegerField(required=False, allow_null=True)
    condition = serializers.ChoiceField(choices=Item.CONDITION_CHOICES, required=False, default='good')
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_category_id(self, value):
        if value:
            try:
                Category.objects.get(id=value)
            except Category.DoesNotExist:
                raise serializers.ValidationError("Category not found")
        return value

    def validate(self, data):
        if data['deposit_amount'] > data['price']:
            raise serializers.ValidationError("Deposit amount cannot exceed the price")
        return data


class QuickLiveAuctionSerializer(serializers.Serializer):
    """Start a 2-minute live auction for a product — same one-tap spirit as
    QuickShowItemSerializer, but viewers bid instead of instantly reserving.
    A minimal backing Item is created the same way (see _create_quick_live_item)."""
    title = serializers.CharField(max_length=200)
    starting_price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    minimum_increment = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    bid_deposit = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.00'))
    quantity = serializers.IntegerField(default=1, min_value=1)
    category_id = serializers.IntegerField(required=False, allow_null=True)
    condition = serializers.ChoiceField(choices=Item.CONDITION_CHOICES, required=False, default='good')
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_category_id(self, value):
        if value:
            try:
                Category.objects.get(id=value)
            except Category.DoesNotExist:
                raise serializers.ValidationError("Category not found")
        return value


class LiveAuctionSerializer(serializers.ModelSerializer):
    """Lightweight live-auction state for the broadcast/viewer overlay —
    current price, minimum next bid, deposit, and time left. Full bid
    history/detail still goes through the regular AuctionListingSerializer
    and /auctions/{id}/bids/ if a client wants it."""
    item_title = serializers.CharField(source='item.title', read_only=True)
    item_image = serializers.SerializerMethodField()
    total_bids = serializers.SerializerMethodField()
    minimum_next_bid = serializers.SerializerMethodField()

    class Meta:
        model = AuctionListing
        fields = [
            'id', 'item_title', 'item_image', 'starting_price', 'current_price',
            'minimum_increment', 'minimum_next_bid', 'bid_deposit', 'status',
            'end_time', 'total_bids',
        ]

    def get_item_image(self, obj):
        first = obj.item.images.order_by('order').first()
        if not first:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(first.image.url) if request else first.image.url

    def get_total_bids(self, obj):
        return obj.bids.count()

    def get_minimum_next_bid(self, obj):
        return obj.current_price + obj.minimum_increment


class StoreChatCreateSerializer(serializers.Serializer):
    """Create or get a direct (store-level) chat with a store"""
    store_id = serializers.IntegerField()
    initial_message = serializers.CharField(required=False, allow_blank=True)

    def validate_store_id(self, value):
        try:
            UserStore.objects.get(id=value)
            return value
        except UserStore.DoesNotExist:
            raise serializers.ValidationError("Store not found")

    def validate(self, data):
        request = self.context.get('request')
        store = UserStore.objects.get(id=data['store_id'])
        if store.user == request.user:
            raise serializers.ValidationError("You cannot chat with your own store")
        return data


class SupportChatCreateSerializer(serializers.Serializer):
    """Create or get the current user's one support conversation — no id
    needed, there's exactly one fixed counterparty (core/services/support.py)."""
    initial_message = serializers.CharField(required=False, allow_blank=True)


from .models import Wallet, DepositRequest, WithdrawalRequest


class WalletSerializer(serializers.ModelSerializer):
    available_balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Wallet
        fields = ['balance', 'held_balance', 'available_balance', 'updated_at']


class DepositCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('1.00'))
    card_last4 = serializers.CharField(max_length=4, required=False, allow_blank=True)


class DepositRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepositRequest
        fields = ['id', 'amount', 'status', 'card_last4', 'reference', 'created_at', 'completed_at']


class WithdrawalCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('1.00'))
    card_last4 = serializers.CharField(max_length=4, required=False, allow_blank=True)


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = WithdrawalRequest
        fields = ['id', 'username', 'amount', 'status', 'card_last4',
                  'admin_notes', 'created_at', 'reviewed_at']


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'notification_type', 'title', 'body', 'data', 'is_read', 'created_at']


# ─── Product Reviews ────────────────────────────────────────────────────────

class ProductReviewSerializer(serializers.ModelSerializer):
    """Read serializer for product reviews"""
    reviewer = UserSerializer(read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductReview
        fields = ['id', 'reviewer', 'item', 'score', 'comment', 'image', 'image_url', 'created_at', 'updated_at']
        read_only_fields = ['reviewer', 'created_at', 'updated_at']

    def get_image_url(self, obj):
        if obj.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None


class ProductReviewCreateSerializer(serializers.ModelSerializer):
    """Write serializer for creating/updating product reviews"""

    class Meta:
        model = ProductReview
        fields = ['item', 'score', 'comment', 'image']

    def validate_score(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError("يجب أن يكون التقييم بين 1 و 5")
        return value

    def validate(self, data):
        request = self.context.get('request')
        item = data.get('item')
        if item and item.owner == request.user:
            raise serializers.ValidationError("لا يمكنك تقييم منتجك الخاص")
        return data

    def create(self, validated_data):
        return ProductReview.objects.create(reviewer=self.context['request'].user, **validated_data)


# ─── Store Reviews ───────────────────────────────────────────────────────────

class StoreReviewSerializer(serializers.ModelSerializer):
    """Read serializer for store reviews"""
    reviewer = UserSerializer(read_only=True)

    class Meta:
        model = StoreReview
        fields = ['id', 'reviewer', 'store', 'score', 'comment', 'created_at', 'updated_at']
        read_only_fields = ['reviewer', 'created_at', 'updated_at']


class StoreReviewCreateSerializer(serializers.ModelSerializer):
    """Write serializer for creating/updating store reviews"""

    class Meta:
        model = StoreReview
        fields = ['store', 'score', 'comment']

    def validate_score(self, value):
        if not (1 <= value <= 5):
            raise serializers.ValidationError("يجب أن يكون التقييم بين 1 و 5")
        return value

    def validate(self, data):
        request = self.context.get('request')
        store = data.get('store')
        if store and store.user == request.user:
            raise serializers.ValidationError("لا يمكنك تقييم متجرك الخاص")
        return data

    def create(self, validated_data):
        return StoreReview.objects.create(reviewer=self.context['request'].user, **validated_data)
        read_only_fields = ['id', 'notification_type', 'title', 'body', 'data', 'created_at']