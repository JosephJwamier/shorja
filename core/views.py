"""
ReSell Trust - Django REST Framework ViewSets (Fixed)

"""
import hashlib
import logging
import uuid
from django.conf import settings
from .wallet_utils import (
    MAX_AMOUNT, undo_sold_count,
    credit_earnings,
    debit_balance,
    refund_to_buyer,
    release_reservation_payment,
    complete_reservation,
    ItemWithdrawnError,
    OutOfStockError,
    cancel_pending_commission,
    forfeit_deposit,
    release_promotions,
    settle_cancelled_auction_win,
    park_if_unlisted,
)
from .services.disputes import add_reply, resolve_dispute, DisputeResolutionError
from .services.open_sellers import is_open, open_seller_q
from .services.refusals import FULL_PAYMENT_ONLY, deposit_refused_to
from .services.wholesale import (
    NOTICE, WholesaleError, claim_handover, confirm_received, mark_notice_seen, needs_notice,
    refund_rest, request_refund,
)
from .services.withdrawals import (
    WithdrawalReviewError, approve_withdrawal, cancel_withdrawal, not_withdrawable,
    reject_withdrawal, tell_reviewers, withdrawal_refusal,
)
from .services.blocking import CANNOT_MESSAGE, blocked_between
from .services.support import SUPPORT_USERNAME, start_support_chat
from .services.moderation import archive_item, resolve_report, ReportResolutionError
from .api_errors import server_error
from .querysets import StableOrderingFilter
from .delivery_utils import resolve_pickup
from .one_at_a_time import one_at_a_time

logger = logging.getLogger(__name__)
from .models import get_or_create_wallet
from rest_framework import viewsets, status, filters, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny,IsAdminUser
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, F, Exists, OuterRef, Prefetch
from django.db.models.functions import Greatest
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.contrib.auth import get_user_model
from datetime import timedelta
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from decimal import Decimal


class ItemStandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
from .models import (ItemImage, ItemVariant, Location,
    Item, FixedPriceListing, AuctionListing, Bid, UserBlock, chat_between,
    Reservation, Representative, ItemTransfer, Category,
    UserStore, Rating, Transaction, Chat, Message, Dispute, Report, Notification,
    ProductReview, StoreReview, StoreFollow, StoreCoupon, CouponCollection,
    LiveSession, LiveSessionItem, LiveSessionLike, LiveSessionComment, LiveSessionViewer,
    LiveSessionModerator, LiveSessionBan,
    UserCategoryAffinity, record_interest,
    Reel, ReelLike, ReelComment, ReelView, SavedReel, Favorite,
)

from .serializers import (store_name_of, ItemImageSerializer, FixedPriceListingCreateSerializer,
                          ItemSerializer, ItemCreateSerializer, ItemListSerializer,
                          FixedPriceListingCardSerializer, AuctionListingCardSerializer,
                          AuctionListingCreateSerializer,
                          ResolveDisputeSerializer,
                          FixedPriceListingSerializer, AuctionListingSerializer,
                          BidSerializer, CreateBidSerializer, CreateDisputeSerializer,
                          ReservationSerializer, CreateReservationSerializer,
                          RepresentativeSerializer, RepresentativeCreateSerializer,
                          ItemTransferSerializer,
                          ResaleAgreementCreateSerializer,
                          ResaleRejectSerializer,
                          ChatSerializer, MessageSerializer,
                          RatingSerializer, RatingCreateSerializer,
                          DisputeSerializer,
                          ReportSerializer, CreateReportSerializer, ResolveReportSerializer,
                          TransactionSerializer, CategorySerializer,
                          UserStoreSerializer, UserSerializer, RepresentativeAcceptSerializer,
                          RepresentativeRejectSerializer, RepresentativeUpdatePermissionsSerializer,
                          ChatDetailSerializer, ChatCreateSerializer,
                          MessageCreateSerializer, MessageOfferSerializer,
                          MESSAGES_PAGE_SIZE,
                          UserStoreCreateSerializer, UserStoreUpdateSerializer,
                          LocationSerializer, LocationCreateUpdateSerializer,
                          NotificationSerializer,
                          ProductReviewSerializer, ProductReviewCreateSerializer,
                          StoreReviewSerializer, StoreReviewCreateSerializer,
                          StoreCouponSerializer, CouponCollectionSerializer, StoreChatCreateSerializer,
                          SupportChatCreateSerializer,
                          LiveSessionCardSerializer, LiveSessionSerializer, LiveSessionCreateSerializer,
                          LiveSessionItemSerializer, LiveSessionItemCreateSerializer,
                          ReserveLiveItemSerializer, LiveSessionCommentSerializer,
                          LiveSessionCommentCreateSerializer, LiveSessionViewerSerializer,
                          LiveSessionModeratorSerializer, AddLiveSessionModeratorSerializer,
                          LiveSessionBanSerializer, BanLiveSessionViewerSerializer, ShowLiveItemSerializer,
                          QuickShowItemSerializer, QuickLiveAuctionSerializer,
                          LiveAuctionSerializer,
                          ReelCardSerializer, ReelDetailSerializer,
                          RequestReelUploadSerializer, FinalizeReelSerializer,
                          ReelCommentSerializer, ReelCommentCreateSerializer,
                          ReelCommentThreadSerializer,
                          )
from .permissions import IsOwnerOrReadOnly, IsParticipantOrReadOnly, IsStoreOwner
from .firestore import sync_chat_to_firestore, sync_message_to_firestore, sync_user_fcm_token
from . import aws_utils
from .filters import (
    ItemFilter, FixedPriceListingFilter, AuctionListingFilter,
    ReservationFilter, ItemTransferFilter, RatingFilter,
    DisputeFilter, ReportFilter, TransactionFilter, UserStoreFilter, LocationFilter,
    ProductReviewFilter, StoreReviewFilter, LiveSessionFilter, ReelFilter,
)
from .iraq_governorates import IRAQ_GOVERNORATES
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .auth_tokens import revoke_refresh_tokens
import math

User = get_user_model()

from rest_framework import permissions


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in km between two GPS coordinates using Haversine formula."""
    R = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def announce_live(session):
    """Tells the store's followers, at most once in a while: a stream stopped
    and restarted (a dropped connection, or a seller after attention) is one."""
    from .tasks import notify_store_followers_live
    if cache.add(f'live-announced:{session.store_id}', 1, 2 * 3600):
        notify_store_followers_live.delay(session.id)


STORE_CLOSED = 'متجرك مغلق — افتحه أولاً لتبدأ البث'


NO_COURIER = 'الدفع بالعربون غير متاح حالياً لعدم وجود شركة توصيل — ادفع المبلغ كاملاً'


def _no_pickup_address():
    """A driver was sent to nowhere: the order went out with a blank address."""
    return Response({
        'success': False,
        'code': 'no_pickup_address',
        'message': 'أضف عنوانك أولاً من «عناويني» ليعرف السائق من أين يستلم'
    }, status=status.HTTP_400_BAD_REQUEST)


def _page_of(request, queryset, size, most):
    """(rows, the next page's number or None) for a list that answers
    {'data': [...]} rather than DRF's page."""
    def number(name, default, top):
        try:
            return max(1, min(int(request.query_params.get(name, default)), top))
        except (TypeError, ValueError):
            return default
    size, page = number('page_size', size, most), number('page', 1, 10_000)
    start = (page - 1) * size
    rows = list(queryset[start:start + size + 1])
    return rows[:size], page + 1 if len(rows) > size else None


def _limit(request, default=50, most=200):
    """?limit= as a sane number, whatever was typed."""
    try:
        return max(1, min(int(request.query_params.get('limit', default)), most))
    except (TypeError, ValueError):
        return default


# A map shows its closest pins; an unbounded radius asked for every row.
MAX_NEARBY = 100
MAX_NEARBY_SCANNED = 2000


def filter_by_proximity(queryset, lat, lng, radius_km, location_attr):
    """Filter a queryset by proximity using a bounding box (DB) then Haversine (Python)."""
    lat, lng, radius_km = float(lat), float(lng), float(radius_km)
    lat_rad = math.radians(lat)
    cos_lat = math.cos(lat_rad) or 1e-10
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * cos_lat)

    queryset = queryset.filter(**{
        f'{location_attr}__latitude__isnull': False,
        f'{location_attr}__latitude__gte': lat - lat_delta,
        f'{location_attr}__latitude__lte': lat + lat_delta,
        f'{location_attr}__longitude__gte': lng - lng_delta,
        f'{location_attr}__longitude__lte': lng + lng_delta,
    })

    result = []
    attrs = location_attr.split('__')
    for obj in queryset[:MAX_NEARBY_SCANNED]:
        loc = obj
        for attr in attrs:
            loc = getattr(loc, attr, None)
            if loc is None:
                break
        if loc and loc.latitude and loc.longitude:
            dist = haversine_distance(lat, lng, float(loc.latitude), float(loc.longitude))
            if dist <= radius_km:
                obj.distance_km = round(dist, 2)
                result.append(obj)

    return sorted(result, key=lambda x: x.distance_km)[:MAX_NEARBY]

class IsSuperAdmin(permissions.BasePermission):
    """
    Custom permission to only allow superadmins to create/edit/delete.
    """

    def has_permission(self, request, view):
        # Allow read permissions for any request
        if request.method in permissions.SAFE_METHODS:
            return True

        # Write permissions only for superadmin
        return request.user and request.user.is_superuser


class UserViewSet(viewsets.ModelViewSet):
    """User management"""
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.is_staff:
            return User.objects.all()
        return User.objects.filter(id=self.request.user.id)

    def create(self, request, *args, **kwargs):
        # Accounts are made by /auth/register/ and the social sign-in, which
        # carry the password policy, the throttle and the phone rules. The
        # router's plain POST had none of them: any signed-in user could mint
        # rows holding someone else's phone or email, and both are unique, so
        # the real owner could then never register.
        return Response({
            'success': False,
            'message': 'أنشئ الحساب من شاشة التسجيل'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        # A raw hard-delete here would CASCADE through owned_items and
        # everything that in turn references those items (reservations,
        # disputes, delivery orders, transfers...) — including other users'
        # records. Account removal must go through delete-account, which
        # deactivates + scrubs PII instead of deleting the row.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف الحساب مباشرة. استخدم /users/delete-account/ لحذف حسابك.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=False, methods=['get'])
    def me(self, request):
        """Get current user profile"""
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get all items owned by a user"""
        user = self.get_object()
        items = Item.objects.filter(owner=user)
        serializer = ItemSerializer(items, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='update-fcm-token')
    def update_fcm_token(self, request):
        """Register or update the Firebase Cloud Messaging (FCM) device token"""
        fcm_token = str(request.data.get('fcm_token') or '').strip()
        if not fcm_token:
            return Response({'success': False, 'message': 'رمز الإشعارات مطلوب'}, status=status.HTTP_400_BAD_REQUEST)
        # A token is a device, and a device has one person signed in: whoever
        # used it before (and left without a clean sign-out) stops receiving
        # their private notifications on someone else's screen.
        for previous in User.objects.filter(fcm_token=fcm_token).exclude(pk=request.user.pk):
            previous.fcm_token = None
            previous.save(update_fields=['fcm_token'])
            sync_user_fcm_token(previous)
        request.user.fcm_token = fcm_token
        request.user.save(update_fields=['fcm_token'])
        sync_user_fcm_token(request.user)
        return Response({'success': True, 'message': 'حُدّث رمز الإشعارات'})

    # Seed weight for an explicitly-chosen interest — higher than a single
    # behavioral signal (purchase +3, follow +1) since the user stated it directly.
    INTEREST_SEED_WEIGHT = 5

    @action(detail=False, methods=['post'], url_path='interests')
    def interests(self, request):
        """Save the categories the user picked on the (skippable) onboarding
        screen. An empty/omitted list just clears them — this is optional,
        not required to use the app. Seeds UserCategoryAffinity so
        recommendations have signal from the very first session."""
        category_ids = request.data.get('category_ids', [])
        if not isinstance(category_ids, list):
            return Response(
                {'success': False, 'message': 'category_ids يجب أن تكون قائمة'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        categories = list(Category.objects.filter(id__in=category_ids, is_active=True))
        request.user.interests.set(categories)

        for category in categories:
            record_interest(request.user, category, weight=self.INTEREST_SEED_WEIGHT)

        return Response({
            'success': True,
            'message': 'تم حفظ اهتماماتك',
            'data': {'category_ids': [c.id for c in categories]},
        })

    @action(detail=False, methods=['post'], url_path='remove-fcm-token')
    def remove_fcm_token(self, request):
        """Remove FCM token (e.g. on logout)"""
        request.user.fcm_token = None
        request.user.save(update_fields=['fcm_token'])
        sync_user_fcm_token(request.user)
        return Response({'success': True, 'message': 'أُزيل رمز الإشعارات'})

    @action(detail=False, methods=['post'], url_path='delete-account')
    def delete_account(self, request):
        """Self-service account deletion (App Store / Play Store 5.1.1(v) requirement).

        Soft-deletes rather than hard-deletes: Transaction/Reservation/Dispute
        rows reference this user and must survive for the other party's
        records and for financial audit trail, so the row stays but the
        account is deactivated, every outstanding session is revoked, and
        personally-identifying fields are scrubbed. Blocked while a wallet
        balance or an in-flight reservation/dispute still exists, so nothing
        involving real money is orphaned mid-flow.
        """
        user = request.user

        wallet = getattr(user, 'wallet', None)
        if wallet and wallet.balance > 0:
            return Response(
                {'success': False, 'message': 'يوجد رصيد في محفظتك — يرجى سحبه بالكامل أولًا قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_open_reservation = Reservation.objects.filter(
            Q(buyer=user) | Q(item__owner=user),
            status__in=['pending', 'completed', 'disputed'],
        ).exists()
        if has_open_reservation:
            return Response(
                {'success': False, 'message': 'لديك حجوزات قيد التنفيذ — يرجى إتمامها أو إلغاؤها أولًا قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if AuctionListing.objects.filter(status='active').filter(
                Q(item__owner=user) | Q(bids__bidder=user)).exists():
            return Response(
                {'success': False, 'message': 'لديك مزاد جارٍ أو مزايدة قائمة — انتظر انتهاء المزاد قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if Transaction.objects.filter(user=user, transaction_type='commission', status='pending').exists():
            return Response(
                {'success': False, 'message': 'لديك عمولة مندوب لم تُحرَّر بعد — انتظر تسويتها قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_open_dispute = Dispute.objects.filter(
            Q(reservation__buyer=user) | Q(reservation__item__owner=user),
            status__in=['open', 'investigating'],
        ).exists()
        if has_open_dispute:
            return Response(
                {'success': False, 'message': 'لديك نزاع مفتوح — يجب إغلاقه أولًا قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .services.wholesale import holds_others_goods
        if holds_others_goods(user):
            return Response(
                {'success': False, 'message': 'لديك اتفاقية جملة لم تُسوَّ: بضاعة أو ثمن بينك وبين طرف آخر — أنهِها أولًا قبل حذف الحساب.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Last, so a refusal above does not use up a WhatsApp code.
        from .identity_proof import require_proof
        require_proof(request)

        with transaction.atomic():
            # Refresh tokens aren't checked against is_active by default, so a
            # deactivated user could otherwise still mint fresh access tokens.
            revoke_refresh_tokens(user)

            anon_suffix = uuid.uuid4().hex[:12]
            user.is_active = False
            user.username = f'deleted-user-{anon_suffix}'
            user.email = ''
            user.first_name = ''
            user.last_name = ''
            user.phone = ''
            user.fcm_token = None
            user.firebase_uid = None
            user.set_unusable_password()
            user.save()
            Item.objects.filter(owner=user).exclude(status='sold').update(status='archived')
            # close_store, not a bare update: a stream on air ends with it.
            from .services.moderation import close_store
            for store in UserStore.objects.filter(user=user):
                close_store(store)

        return Response({'success': True, 'message': 'تم حذف الحساب بنجاح.'})


class ItemViewSet(viewsets.ModelViewSet):
    """Item CRUD and listing"""
    queryset = (
        Item.objects
        .all()
        .select_related('owner', 'current_holder', 'category')
        .prefetch_related('images', 'location_history')
        .order_by('-created_at', '-id')
    )
    serializer_class = ItemSerializer
    permission_classes = [IsAuthenticated]
    # JSONParser added for variants: nested option/variant structures are
    # natural JSON, while images still arrive as multipart. Multipart
    # callers send `options`/`variants` as JSON-encoded strings — the
    # serializer decodes those.
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_class = ItemFilter
    search_fields = ['title', 'name_ar', 'description']
    ordering_fields = ['created_at', 'updated_at', 'sold_count', 'rating']
    pagination_class = ItemStandardPagination

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action not in ('list', 'retrieve'):
            return queryset
        queryset = item_related(queryset)
        user = self.request.user
        if user.is_authenticated and user.is_staff:
            return queryset
        # Not published, or taken down: its owner's, and whoever has an order
        # on it (their order page still opens it). Nobody else's.
        visible = ~Q(status__in=['draft', 'archived']) & open_seller_q('owner')
        if user.is_authenticated:
            visible |= Q(owner=user) | Q(current_holder=user) | Q(
                Exists(Reservation.objects.filter(item=OuterRef('pk'), buyer=user)))
        return queryset.filter(visible)

    def get_serializer_class(self):
        if self.action == 'create':
            return ItemCreateSerializer
        return ItemSerializer

    def get_permissions(self):
        # Browsing is open: a visitor must be able to see what is for sale
        # before deciding whether the app is worth an account. Everything
        # that writes, reserves, bids or reveals another user's data stays
        # behind IsAuthenticated — only these two read actions are opened.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        # Base update/delete have no manual owner check (unlike the custom
        # actions below, which each enforce their own owner/holder rules) —
        # restrict them here so only the item's owner can edit or remove it.
        if self.action in ('update', 'partial_update', 'destroy'):
            return [IsAuthenticated(), IsOwnerOrReadOnly()]
        return super().get_permissions()

    def destroy(self, request, *args, **kwargs):
        # Never hard-delete: Item is CASCADE-referenced by Reservation,
        # ItemTransfer, Dispute (via Reservation), DeliveryOrder (via
        # Reservation), ProductReview, Chat and more — a real delete would
        # silently wipe financial/legal history tied to this listing. Reuse
        # the same archive_item() the admin moderation action calls, so a
        # seller "deleting" their own item behaves identically to an admin
        # archiving it (item.status='archived', pulled from listings, and
        # every other model just sees the item it already knows about).
        item = self.get_object()

        if self._auction_is_being_bid_on(item):
            return Response({
                'success': False,
                'message': 'لا يمكن حذف المنتج وعليه مزايدات — أنهِ المزاد أولاً',
            }, status=status.HTTP_400_BAD_REQUEST)

        if item.status != 'archived':
            reason = (request.data.get('reason') or '').strip() or 'حذف بواسطة المالك عبر التطبيق'
            archive_item(item, reason=reason, actor=request.user)

        return Response({
            'success': True,
            'message': 'تم حذف المنتج بنجاح',
            'data': None,
        })

    @staticmethod
    def _auction_is_being_bid_on(item):
        return Bid.objects.filter(auction__item=item, auction__status='active').exists()

    def update(self, request, *args, **kwargs):
        # People have put money on what the listing says it is.
        if self._auction_is_being_bid_on(self.get_object()):
            return Response({
                'success': False,
                'message': 'لا يمكن تعديل المنتج وعليه مزايدات',
            }, status=status.HTTP_400_BAD_REQUEST)
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        """
        Restocking a sold-out item relists it automatically: if it was 'sold'
        and the edit leaves it with quantity > 0, flip it back to 'active'.
        `status` is read-only on the serializer, so this is the only path
        back to 'active' after a wholesale transfer zeroed out the stock.
        """
        was_sold = serializer.instance.status == 'sold'
        item = serializer.save()
        if was_sold and item.quantity > 0:
            item.status = 'active'
            item.save(update_fields=['status'])
    #
    # def get_parsers(self):
    #     """
    #     Use form parsers for create and upload_images, JSON parser for everything else
    #     """
    #     action = getattr(self, 'action', None)
    #     if action in ['create', 'upload_images']:
    #         return [MultiPartParser(), FormParser()]
    #     return [JSONParser()]

    def handle_image_uploads(self, item, request):
        """Handle multiple image uploads"""
        images = request.FILES.getlist('images')
        created_images = []
        errors = []

        # For the product, not for each request to add some.
        room = max(MAX_IMAGES_PER_REQUEST - item.images.count(), 0)
        if len(images) > room:
            errors.append('%d صورة للمنتج كحد أقصى' % MAX_IMAGES_PER_REQUEST)
            images = images[:room]

        for idx, image in enumerate(images):
            try:
                validate_image(image)
                item_image = ItemImage.objects.create(
                    item=item,
                    image=image,
                    order=idx
                )
                created_images.append(item_image)
            except DjangoValidationError as exc:
                errors.append(exc.messages[0])
            except Exception:
                # The storage error's text (bucket, endpoint) is for the log.
                logger.exception('Image %s of item %s failed to upload', idx + 1, item.id)
                errors.append(f"Failed to upload image {idx + 1}")

        return created_images, errors

    @swagger_auto_schema(
        operation_description="Create a new item with images",
        request_body=ItemCreateSerializer,
        manual_parameters=[
            openapi.Parameter(
                'images',
                openapi.IN_FORM,
                type=openapi.TYPE_ARRAY,
                items=openapi.Items(type=openapi.TYPE_FILE),
                required=False,
                description='Upload multiple images for the item'
            )
        ],
        responses={
            201: ItemSerializer,
            400: "Validation failed",
            500: "Internal server error"
        }
    )
    def create(self, request, *args, **kwargs):
        """Create item with images using transaction"""
        # request.data is a QueryDict for multipart and a dict for JSON;
        # copy without flattening so nested variants survive intact.
        form_data = {k: v for k, v in request.data.items() if k != 'images'}

        # An item with nowhere to be collected from produces a delivery
        # order with an empty from_address — the driver is dispatched to
        # nothing, and compute_delivery_fee silently falls back to the
        # inter-governorate rate. Refuse at the only moment the seller can
        # still fix it cheaply, and tell the client exactly what is missing
        # so it can open the location sheet instead of guessing.
        from .delivery_utils import _party_location
        if _party_location(request.user) is None:
            return Response({
                'success': False,
                'code': 'no_pickup_location',
                'message': 'أضف موقعك الرئيسي أولاً — منه تستلم شركة التوصيل منتجاتك.',
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            serializer = self.get_serializer(data=form_data)

            if serializer.is_valid():
                with transaction.atomic():
                    item = serializer.save(
                        owner=request.user,
                        current_holder=request.user
                    )

                    created_images = []
                    image_errors = []

                    if request.FILES.getlist('images'):
                        created_images, image_errors = self.handle_image_uploads(item, request)
                        if image_errors:
                            print(f"Image upload errors for item {item.id}: {image_errors}")

                response_serializer = ItemSerializer(item, context={'request': request})

                message = 'أُضيف المنتج'
                if created_images:
                    message += f' with {len(created_images)} image(s)'
                if image_errors:
                    message += f', but {len(image_errors)} image(s) failed to upload'

                return Response({
                    'success': True,
                    'message': message,
                    'data': response_serializer.data,
                    'images_uploaded': len(created_images),
                    'image_errors': image_errors if image_errors else None
                }, status=status.HTTP_201_CREATED)

            else:
                return Response({
                    'success': False,
                    'message': 'البيانات غير مكتملة — راجع الحقول المطلوبة',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception:
            return server_error('تعذّرت إضافة المنتج')

    @swagger_auto_schema(
        operation_description="Create a fixed price listing for an item",
        request_body=FixedPriceListingCreateSerializer,

        responses={
            201: FixedPriceListingSerializer,
            400: "Validation failed",
            403: "Permission denied",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['post'])
    def create_fixed_listing(self, request, pk=None):
        """Create a fixed price listing for an item"""
        item = self.get_object()

        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'العرض للبيع لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if item.status != 'draft':
            return Response({
                'success': False,
                'message': 'لا يمكن عرض المنتج — يُعرض المنتج المسوّدة فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        if hasattr(item, 'fixed_listing'):
            return Response({
                'success': False,
                'message': 'المنتج معروض بسعر ثابت مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = FixedPriceListingCreateSerializer(data=request.data)
        if serializer.is_valid():
            try:
                with transaction.atomic():
                    listing = serializer.save(item=item)
                    item.status = 'active'
                    item.save()

                return Response({
                    'success': True,
                    'message': 'عُرض المنتج للبيع',
                    'data': FixedPriceListingSerializer(listing).data
                }, status=status.HTTP_201_CREATED)

            except Exception:
                return server_error('تعذّر عرض المنتج')

        return Response({
            'success': False,
            'message': 'بيانات غير صالحة',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="Update the fixed price listing for an item",
        request_body=FixedPriceListingCreateSerializer,
        responses={
            200: FixedPriceListingSerializer,
            400: "Validation failed",
            403: "Permission denied",
            404: "Listing not found",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['patch'])
    def update_fixed_listing(self, request, pk=None):
        """Update the fixed price listing for an item"""
        item = self.get_object()

        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'تعديل العرض لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if not hasattr(item, 'fixed_listing'):
            return Response({
                'success': False,
                'message': 'المنتج غير معروض بسعر ثابت'
            }, status=status.HTTP_404_NOT_FOUND)

        listing = item.fixed_listing
        serializer = FixedPriceListingSerializer(listing, data=request.data, partial=True)

        if serializer.is_valid():
            try:
                with transaction.atomic():
                    updated_listing = serializer.save()

                return Response({
                    'success': True,
                    'message': 'حُدّث السعر',
                    'data': FixedPriceListingSerializer(updated_listing).data
                }, status=status.HTTP_200_OK)

            except Exception:
                return server_error('تعذّر تحديث العرض')

        return Response({
            'success': False,
            'message': 'بيانات غير صالحة',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        method='post',
        operation_description="Upload additional images to an existing item",
        manual_parameters=[
            openapi.Parameter(
                'images',
                openapi.IN_FORM,
                type=openapi.TYPE_ARRAY,
                items=openapi.Items(type=openapi.TYPE_FILE),
                required=True,
                description='Upload one or more images'
            )
        ]
    )
    @action(detail=True, methods=['post'])  # Remove parser_classes from here
    def upload_images(self, request, pk=None):
        """Upload additional images for this item"""
        item = self.get_object()

        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'رفع الصور لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        images = request.FILES.getlist('images')
        if not images:
            return Response({
                'success': False,
                'message': 'لم تُرفق أي صورة'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                created_images, errors = self.handle_image_uploads(item, request)

            if not created_images:
                return Response({
                    'success': False,
                    'message': errors[0] if errors else 'تعذّر رفع الصور',
                    'image_errors': errors,
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                'success': True,
                'message': f'رُفعت {len(created_images)} صورة',
                'images_uploaded': len(created_images),
                'image_errors': errors if errors else None,
                'data': ItemImageSerializer(created_images, many=True).data
            }, status=status.HTTP_201_CREATED)
        except Exception:
            return server_error('تعذّر رفع الصور')

    @swagger_auto_schema(
        operation_description="Create an auction listing for an item (only for single quantity items)",
        request_body=AuctionListingCreateSerializer,
        responses={
            201: AuctionListingSerializer,
            400: "Validation failed",
            403: "Permission denied",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['post'])
    def create_auction(self, request, pk=None):
        """Create an auction for an item"""
        item = self.get_object()

        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'إنشاء المزاد لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Check if item quantity is 1
        if item.quantity != 1:
            return Response({
                'success': False,
                'message': f'المزاد لقطعة واحدة فقط، وكمية هذا المنتج {item.quantity}'
            }, status=status.HTTP_400_BAD_REQUEST)

        if item.status != 'draft':
            return Response({
                'success': False,
                'message': 'لا يمكن إنشاء مزاد — يُنشأ للمنتج المسوّدة فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = AuctionListingCreateSerializer(data=request.data)

        if serializer.is_valid():
            try:
                with transaction.atomic():
                    auction = serializer.save(
                        item=item,
                        current_price=serializer.validated_data['starting_price'],
                        status='active'
                    )

                    item.status = 'active'
                    item.save(update_fields=['status'])

                return Response({
                    'success': True,
                    'message': 'أُنشئ المزاد',
                    'data': AuctionListingSerializer(auction).data
                }, status=status.HTTP_201_CREATED)

            except Exception:
                return server_error('تعذّر إنشاء المزاد')

        return Response({
            'success': False,
            'message': 'بيانات غير صالحة',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)


    @swagger_auto_schema(
        operation_description="Update an auction listing for an item",
        request_body=AuctionListingCreateSerializer,
        responses={
            200: AuctionListingSerializer,
            400: "Validation failed",
            403: "Permission denied",
            404: "Auction not found",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['patch'], url_path='update_auction/(?P<auction_id>[^/.]+)')
    def update_auction(self, request, pk=None, auction_id=None):
        """Update an existing auction for an item"""
        item = self.get_object()

        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'تعديل المزاد لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            auction = AuctionListing.objects.get(id=auction_id, item=item)
        except AuctionListing.DoesNotExist:
            return Response({
                'success': False,
                'message': 'لا يوجد مزاد لهذا المنتج'
            }, status=status.HTTP_404_NOT_FOUND)

        # Only allow updating upcoming or active auctions
        if auction.status == 'ended':
            return Response({
                'success': False,
                'message': 'لا يمكن تعديل مزاد منتهٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Prevent updating if bids already exist
        if auction.bids.exists():
            return Response({
                'success': False,
                'message': 'لا يمكن تعديل مزاد عليه مزايدات'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = AuctionListingCreateSerializer(auction, data=request.data, partial=True)

        if serializer.is_valid():
            try:
                with transaction.atomic():
                    # Recalculate status based on new start_time/end_time
                    now = timezone.now()
                    start_time = serializer.validated_data.get('start_time', auction.start_time)
                    end_time = serializer.validated_data.get('end_time', auction.end_time)

                    if end_time <= now:
                        new_status = 'ended'
                    elif start_time <= now:
                        new_status = 'active'
                    else:
                        new_status = 'upcoming'

                    updated_auction = serializer.save(
                        status=new_status,
                        current_price=serializer.validated_data.get(
                            'starting_price', auction.starting_price
                        )
                    )

                return Response({
                    'success': True,
                    'message': 'حُدّث المزاد',
                    'data': AuctionListingSerializer(updated_auction).data
                }, status=status.HTTP_200_OK)

            except Exception:
                return server_error('تعذّر تحديث المزاد')

        return Response({
            'success': False,
            'message': 'بيانات غير صالحة',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="البحث عن منتجات قريبة من إحداثيات معينة",
        manual_parameters=[
            openapi.Parameter('lat', openapi.IN_QUERY, description='خط العرض', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('lng', openapi.IN_QUERY, description='خط الطول', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('radius', openapi.IN_QUERY, description='نصف القطر بالكيلومتر (افتراضي: 10)', type=openapi.TYPE_NUMBER),
        ],
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def nearby(self, request):
        """GET /api/items/nearby/?lat=33.3&lng=44.4&radius=10"""
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        radius = request.query_params.get('radius', 10)
        if not lat or not lng:
            return Response({'success': False, 'message': 'lat و lng مطلوبان'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            queryset = Item.objects.filter(
                open_seller_q('owner'), status='active', current_location__isnull=False
            ).select_related('current_location', 'owner', 'category').prefetch_related('images')
            results = filter_by_proximity(queryset, lat, lng, radius, 'current_location')
            data = ItemSerializer(results, many=True, context={'request': request}).data
            return Response({
                'success': True,
                'message': f'تم العثور على {len(data)} منتج قريب',
                'count': len(data),
                'data': data,
            })
        except (ValueError, TypeError):
            return Response({
                'success': False,
                'message': 'قيم lat/lng/radius غير صالحة',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)


    @action(detail=True, methods=['post'], url_path='update_location')
    def update_location(self, request, pk=None):
        """POST /api/items/{id}/update_location/ — تحديث موقع المنتج"""
        item = self.get_object()

        if request.user not in [item.owner, item.current_holder]:
            return Response({'success': False, 'message': 'ليس لديك صلاحية تحديث موقع هذا المنتج'}, status=status.HTTP_403_FORBIDDEN)

        from .serializers import ItemUpdateLocationSerializer
        serializer = ItemUpdateLocationSerializer(
            data=request.data,
            context={'request': request, 'item': item}
        )
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors,
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)

        current_location_id = serializer.validated_data['current_location_id']
        notes = serializer.validated_data.get('notes', '')
        try:
            # The caller's own locations only. Any id used to be accepted, and
            # an item shows its location's pin and owner to everyone - so
            # walking the ids over one's own item read out where every other
            # user lives, buyers' delivery addresses included.
            location = Location.objects.get(id=current_location_id, user=request.user)
        except (Location.DoesNotExist, TypeError, ValueError):
            return Response({
                'success': False,
                'message': 'الموقع غير موجود',
                'data': None,
            }, status=status.HTTP_404_NOT_FOUND)

        from .models import ItemLocationHistory
        if item.current_location != location:
            ItemLocationHistory.objects.create(
                item=item,
                location=location,
                moved_by=request.user,
                notes=notes
            )
            item.current_location = location
            item.save(update_fields=['current_location'])

        return Response({
            'success': True,
            'message': 'تم تحديث موقع المنتج بنجاح',
            'data': ItemSerializer(item, context={'request': request}).data,
        })

    @action(detail=True, methods=['post'])
    def favorite(self, request, pk=None):
        """POST /api/items/{id}/favorite/ — أضف المنتج إلى المفضلة"""
        item = self.get_object()
        _favorite, created = Favorite.objects.get_or_create(item=item, user=request.user)
        if created:
            # Favoriting is a deliberate, item-specific signal — stronger than
            # following a store (weight=1) but short of an actual purchase
            # (weight=3). Only on first-time favorite so repeated toggle
            # calls (already deduped by get_or_create) can't inflate it.
            record_interest(request.user, item.category, weight=2)
        return Response({
            'success': True,
            'is_favorited': True,
        })

    @action(detail=True, methods=['post'])
    def unfavorite(self, request, pk=None):
        """POST /api/items/{id}/unfavorite/ — أزل المنتج من المفضلة"""
        item = self.get_object()
        Favorite.objects.filter(item=item, user=request.user).delete()
        return Response({
            'success': True,
            'is_favorited': False,
        })


class SafeRankingCacheMixin:
    """قراءة/كتابة ذاكرة مؤقّتة لا تُسقط الطلب حين تسقط هي.

    الترتيبات المحفوظة (قوائم السعر الثابت وخلاصة الريلز) تحسينٌ لا مصدرُ
    حقيقة: Redis غير متاح يجب أن يعني ترتيباً أبطأ لا صفحةً فاشلة.

    وهي مشتركة لا منسوخة: نسختان من نفس المساعد في ملفّ واحد تتباعدان
    بصمت — تُصلَح إحداهما ويُنسى أن للأخرى وجوداً.
    """

    def _cache_get(self, key):
        try:
            return cache.get(key)
        except Exception:
            logger.warning('Ranking cache read failed for key %s', key, exc_info=True)
            return None

    def _cache_set(self, key, value, ttl):
        try:
            cache.set(key, value, ttl)
        except Exception:
            logger.warning('Ranking cache write failed for key %s', key, exc_info=True)


class FixedPriceListingViewSet(SafeRankingCacheMixin, viewsets.ReadOnlyModelViewSet):
    """View fixed price listings — supports category browsing"""
    queryset = (
        FixedPriceListing.objects
        .filter(open_seller_q('item__owner'), is_active=True, item__status__in=['active', 'reserved'])
        .select_related('item', 'item__category', 'item__owner', 'item__current_holder')
        .prefetch_related('item__images', 'item__location_history')
        .order_by('-created_at', '-id')
    )
    serializer_class = FixedPriceListingSerializer
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_class = FixedPriceListingFilter
    search_fields = ['item__title', 'item__name_ar', 'item__description']
    ordering_fields = ['price', 'created_at', 'item__sold_count', 'item__rating']
    pagination_class = ItemStandardPagination

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ('list', 'retrieve'):
            queryset = item_related(queryset, 'item')
        return queryset

    SIMILAR_DEFAULT_LIMIT = 10
    SIMILAR_MAX_LIMIT = 50
    SIMILAR_PRICE_RANGE = Decimal('0.30')  # ±30% price band eligible for a price-proximity score
    SIMILAR_CANDIDATE_POOL = 200  # cap rows pulled per category before scoring in Python
    SIMILAR_CACHE_TTL = 300  # 5 minutes

    RECOMMENDED_TOP_CATEGORIES = 5  # how many of the user's strongest categories to rank by
    # Total distinct listings considered for one user across the whole
    # "عرض الكل" feed (all pages combined) — bounded so computing/caching it
    # stays cheap regardless of how large the catalog grows.
    RECOMMENDED_POOL_SIZE = 300
    # Affinity (and thus this feed) drifts slowly — a longer TTL than `similar`
    # is deliberate: it means most home-screen loads hit the cache instead of
    # re-ranking per request, which is what keeps this cheap at low traffic.
    RECOMMENDED_CACHE_TTL = 1800  # 30 minutes

    @action(detail=True, methods=['get'])
    def similar(self, request, pk=None):
        """Similar listings ranked by category match, price proximity, condition and popularity"""
        # bypass get_object()'s class-level prefetches (images/location history) — unused here
        listing = get_object_or_404(
            FixedPriceListing.objects.filter(is_active=True).select_related('item'),
            pk=pk,
        )
        try:
            limit = int(request.query_params.get('limit', self.SIMILAR_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = self.SIMILAR_DEFAULT_LIMIT
        limit = max(1, min(limit, self.SIMILAR_MAX_LIMIT))

        cache_key = f'similar_listings:{listing.pk}:{limit}'
        cached = self._cache_get(cache_key)
        if cached is not None:
            return Response(cached)

        results = self._similar_listings(listing, limit)
        data = FixedPriceListingSerializer(results, many=True, context={'request': request}).data
        self._cache_set(cache_key, data, self.SIMILAR_CACHE_TTL)
        return Response(data)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def recommended(self, request):
        """Personalized feed ranked by the requesting user's category affinity
        (from onboarding interests + purchases/follows), falling back to
        popular items to fill the page — same lightweight cached/capped-pool
        approach as `similar` above, no live aggregation over history on
        every request. Standard page/page_size pagination (same shape as the
        default listing endpoint) so "عرض الكل" can scroll through it.

        Open to guests, who get the popular fallback the ranking already
        computes for anyone with no affinity yet. It used to be
        IsAuthenticated, which answered 401 — and the home screen's
        recommended strip and the whole "عرض الكل" screen were therefore
        empty for a visitor, on a feed that needs no identity to be useful."""
        ordered_ids = self._recommended_listing_ids(request.user)

        page = self.paginate_queryset(ordered_ids)
        page_ids = page if page is not None else ordered_ids

        # `pk__in` does not preserve order — re-sort the fetched rows to match
        # the ranked id order before serializing.
        listings_by_id = {obj.pk: obj for obj in self.get_queryset().filter(pk__in=page_ids)}
        ordered_page = [listings_by_id[pk] for pk in page_ids if pk in listings_by_id]

        serializer = FixedPriceListingSerializer(ordered_page, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def favorites(self, request):
        """المفضلة — قوائم السعر الثابت للمنتجات التي فضّلها المستخدم، الأحدث تفضيلاً أولاً.

        Ranked by ``Favorite.created_at`` fetched directly from the Favorite
        table rather than via `.order_by('item__favorited_by__created_at')`
        on this viewset's queryset: chaining that order_by after
        `.filter(item__favorited_by__user=...)` risks Django adding the
        ORDER BY join separately from the WHERE join, so for an item
        favorited by more than one user the join could pick a *different*
        user's row and duplicate the listing in the results. Fetching ids
        first (same approach `_recommended_listing_ids` uses above) sidesteps
        that entirely.
        """
        favorited_item_ids = list(
            Favorite.objects.filter(user=request.user)
            # -id as a tiebreaker: two favorites created back-to-back can
            # land on the same created_at value (MySQL datetime(6) is still
            # coarser than how fast two INSERTs can fire), which would
            # otherwise leave "most recent first" order undefined for them.
            .order_by('-created_at', '-id')
            .values_list('item_id', flat=True)
        )

        page = self.paginate_queryset(favorited_item_ids)
        page_ids = page if page is not None else favorited_item_ids

        listings_by_item_id = {
            listing.item_id: listing
            for listing in self.get_queryset().filter(item_id__in=page_ids)
        }
        ordered_page = [listings_by_item_id[i] for i in page_ids if i in listings_by_item_id]

        serializer = FixedPriceListingSerializer(ordered_page, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def _recommended_listing_ids(self, user):
        """Full ranked pool of listing pks for this user (affinity-matched
        first, then popular fallback), cached as plain ids — cheap to store
        and cheap to slice per page, so pagination never re-runs the ranking."""
        # الضيف: لا `id` يُبنى عليه مفتاح، ولا تفضيلات تُقرأ. ومفتاح واحد
        # مشترك لكل الزوّار لا مفتاح لكل زائر — فالقائمة التي يرونها واحدة
        # (الأكثر رواجاً)، فتُحسب مرّة وتُخدَم للجميع.
        authenticated = bool(getattr(user, 'is_authenticated', False))
        cache_key = ('recommended_listing_ids:%s' % user.id if authenticated
                     else 'recommended_listing_ids:anon')
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        top_category_ids = list(
            UserCategoryAffinity.objects.filter(user=user, score__gt=0)
            .order_by('-score').values_list('category_id', flat=True)[:self.RECOMMENDED_TOP_CATEGORIES]
        ) if authenticated else []

        base_qs = self.get_queryset()
        ordered_ids = []
        if top_category_ids:
            rank_by_category = {cat_id: i for i, cat_id in enumerate(top_category_ids)}
            pool = list(
                base_qs.filter(item__category_id__in=top_category_ids)
                .order_by('-item__rating', '-item__sold_count')
                .values_list('pk', 'item__category_id')[:self.RECOMMENDED_POOL_SIZE]
            )
            pool.sort(key=lambda row: rank_by_category.get(row[1], len(rank_by_category)))
            ordered_ids = [pk for pk, _category_id in pool]

        if len(ordered_ids) < self.RECOMMENDED_POOL_SIZE:
            remaining = self.RECOMMENDED_POOL_SIZE - len(ordered_ids)
            fallback_ids = list(
                base_qs.exclude(pk__in=ordered_ids)
                .order_by('-item__rating', '-item__sold_count')
                .values_list('pk', flat=True)[:remaining]
            )
            ordered_ids += fallback_ids

        self._cache_set(cache_key, ordered_ids, self.RECOMMENDED_CACHE_TTL)
        return ordered_ids


    def _similar_listings(self, listing, limit):
        """Rank candidate pks with lightweight queries, then fetch full detail in one shot"""
        item = listing.item
        base_qs = (
            FixedPriceListing.objects
            .filter(is_active=True, item__status='active')
            .exclude(pk=listing.pk)
            .exclude(item__owner_id=item.owner_id)  # "more from this seller" is a separate section
        )

        same_category_pool = list(
            base_qs.filter(item__category_id=item.category_id)
            .select_related('item')
            .only('id', 'price', 'item__id', 'item__condition', 'item__rating', 'item__sold_count')
            .order_by('-item__rating', '-item__sold_count')[:self.SIMILAR_CANDIDATE_POOL]
        )
        ranked_ids = [
            candidate.pk for candidate in sorted(
                same_category_pool,
                key=lambda candidate: self._similar_score(candidate, listing),
                reverse=True,
            )
        ][:limit]

        if len(ranked_ids) < limit:
            fallback_ids = list(
                base_qs.exclude(pk__in=[listing.pk] + ranked_ids)
                .order_by('-item__rating', '-item__sold_count')
                .values_list('pk', flat=True)[:limit - len(ranked_ids)]
            )
            ranked_ids += fallback_ids

        detailed_by_id = {
            obj.pk: obj for obj in
            FixedPriceListing.objects.filter(pk__in=ranked_ids)
            .select_related('item', 'item__category', 'item__owner', 'item__current_holder')
            .prefetch_related('item__images', 'item__location_history')
        }
        return [detailed_by_id[pk] for pk in ranked_ids if pk in detailed_by_id]

    def _similar_score(self, candidate, target_listing):
        candidate_item = candidate.item
        target_item = target_listing.item
        score = Decimal('0')

        if target_listing.price and target_listing.price > 0:
            diff_ratio = abs(candidate.price - target_listing.price) / target_listing.price
            if diff_ratio <= self.SIMILAR_PRICE_RANGE:
                score += (Decimal('1') - diff_ratio / self.SIMILAR_PRICE_RANGE) * Decimal('50')

        if candidate_item.condition == target_item.condition:
            score += Decimal('15')

        score += candidate_item.rating * Decimal('4')  # up to 20
        score += min(candidate_item.sold_count, 100) / Decimal('100') * Decimal('15')  # up to 15

        return score



from .consumers import send_websocket_notification, broadcast_live_event  # Import the utility functions
from .services.live_media import get_live_media_provider
from .services.live_media.base import MediaProviderError
from .live_utils import end_live_session
from .tasks import remind_to_complete_reservation
from .notifications import (
    notify_reservation_created,
    notify_reservation_cancelled_by_seller,
    notify_reservation_cancelled_by_buyer,
    notify_reservation_completed,
    notify_live_item_reserved,
    notify_live_item_sold_out,
    notify_delivery_arrived,
    notify_delivery_refused,
    notify_cash_collected,
    notify_auction_won,
    notify_dispute_created,
    notify_report_submitted,
    notify_report_reviewed,
    notify_new_message,
    notify_reel_liked,
    notify_reel_comment,
    notify_push,
    bidi_isolate,
)
from .models import DeliveryOrder
from .serializers import DeliveryOrderSerializer
from django.core.exceptions import ValidationError as DjangoValidationError
from .uploads import MAX_IMAGES_PER_REQUEST, validate_image
from .querysets import item_related, reservation_related, user_related, with_store_numbers
from .delivery_utils import hand_over_batch_fee, record_receipt
from .delivery_utils import (
    create_outbound_delivery_order,
    create_return_delivery_order,
    execute_return_refund,
    get_active_delivery_company,
    pack_into_delivery_groups,
    combine_deliveries,
)


class AuctionViewSet(viewsets.ReadOnlyModelViewSet):
    """Auction listing views — supports category browsing"""
    queryset = (
        AuctionListing.objects
        .all()
        .select_related('item', 'item__category', 'item__owner', 'item__current_holder', 'winner')
        .prefetch_related('item__images', 'item__location_history', 'bids__bidder')
    )
    serializer_class = AuctionListingSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_class = AuctionListingFilter
    search_fields = ['item__title', 'item__name_ar', 'item__description']
    ordering_fields = ['created_at', 'end_time', 'current_price', 'item__sold_count']
    pagination_class = ItemStandardPagination

    def get_permissions(self):
        # Same reasoning as ItemViewSet: a live auction is the strongest
        # reason to sign up, so a guest has to be able to watch one.
        # my_auctions and place_bid both read request.user and stay closed.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ('list', 'retrieve'):
            queryset = user_related(item_related(queryset, 'item'), 'winner').prefetch_related(
                'bids__bidder__store__badges')
            user = self.request.user
            if not user.is_staff:
                # A suspended seller's: still opened by those who bid on it.
                mine = Q(item__owner=user) | Q(Exists(Bid.objects.filter(
                    auction=OuterRef('pk'), bidder=user))) if user.is_authenticated else Q(pk__in=[])
                queryset = queryset.filter(Q(item__owner__is_active=True) | mine)
        if self.request.query_params.get('active_only') == 'true':
            now = timezone.now()
            queryset = queryset.filter(
                status='active',
                start_time__lte=now,
                end_time__gte=now
            )
        return queryset

    @action(detail=False, methods=['get'], url_path='my_auctions')
    def my_auctions(self, request):
        """Return all auctions where the item owner is the current user"""
        queryset = self.get_queryset().filter(item__owner=request.user)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        operation_description="Place a bid on an auction",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['amount'],
            properties={
                'amount': openapi.Schema(
                    type=openapi.TYPE_NUMBER,
                    description='Bid amount (must be higher than current price + minimum increment)'
                ),
            }
        ),
        responses={
            201: BidSerializer,
            400: "Validation failed",
            403: "Permission denied",
            404: "Auction not found",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['post'])
    def place_bid(self, request, pk=None):
        """Place a bid on an auction"""
        auction = self.get_object()

        # Check if user is the item owner
        if auction.item.owner == request.user:
            return Response({
                'success': False,
                'message': 'لا يمكنك المزايدة على منتجك'
            }, status=status.HTTP_403_FORBIDDEN)

        # Validate bid amount
        serializer = CreateBidSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        bid_amount = serializer.validated_data['amount']

        try:
            with transaction.atomic():
                # Lock the auction row for the rest of this transaction so
                # concurrent bids on the same auction are serialized instead
                # of racing on stale current_price/is_winning reads (the
                # loser of the race would otherwise silently overwrite the
                # winner's price or is_winning flag).
                auction = AuctionListing.objects.select_for_update().select_related(
                    'item', 'item__owner'
                ).get(pk=auction.pk)

                # Check if auction is active (re-checked against the locked row)
                if (not auction.is_active() or not is_open(auction.item.owner)
                        or auction.item.status == 'archived'):
                    return Response({
                        'success': False,
                        'message': 'المزاد غير نشط'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Check minimum bid amount (re-checked against the locked, fresh price)
                minimum_bid = auction.current_price + auction.minimum_increment
                if bid_amount < minimum_bid:
                    return Response({
                        'success': False,
                        'message': f'أقل مزايدة {minimum_bid} د.ع، والسعر الحالي {auction.current_price} د.ع'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # A slip of the thumb, or a bid nobody means to pay, ends the
                # auction for everyone else.
                ceiling = max(auction.current_price * 5,
                              auction.current_price + Decimal('1000000'), minimum_bid)
                if bid_amount > ceiling:
                    return Response({
                        'success': False,
                        'message': f'المزايدة كبيرة جداً — أعلى ما يمكن الآن {ceiling:,.0f} د.ع'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Check if user has already placed a higher or equal bid
                existing_bid = Bid.objects.filter(
                    auction=auction,
                    bidder=request.user
                ).order_by('-amount').first()

                if existing_bid and existing_bid.amount >= bid_amount:
                    return Response({
                        'success': False,
                        'message': f'مزايدتك الحالية {existing_bid.amount} د.ع، زايد بأكثر منها'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Check if this bidder already paid the deposit for this auction
                already_paid_deposit = Bid.objects.filter(
                    auction=auction,
                    bidder=request.user,
                    deposit_paid=True
                ).exists()

                # Charge bid deposit on first bid only
                if not already_paid_deposit and auction.bid_deposit > 0:
                    debit_balance(
                        user=request.user,
                        amount=auction.bid_deposit,
                        transaction_type='deposit',
                        auction=auction
                    )

                # Get previous winning bidder before creating new bid
                previous_winning_bid = Bid.objects.filter(
                    auction=auction,
                    is_winning=True
                ).first()

                # Create the bid — deposit_paid reflects whether deposit was charged
                bid = Bid.objects.create(
                    auction=auction,
                    bidder=request.user,
                    amount=bid_amount,
                    deposit_paid=(already_paid_deposit or auction.bid_deposit > 0),
                    is_winning=True
                )

                # Update all previous bids to not winning
                Bid.objects.filter(
                    auction=auction
                ).exclude(id=bid.id).update(is_winning=False)

                # Update auction current price
                old_price = auction.current_price
                auction.current_price = bid_amount
                auction.save()

                # Get all users who have bid on this auction (to notify them)
                previous_bidders = Bid.objects.filter(
                    auction=auction
                ).exclude(bidder=request.user).values_list('bidder_id', flat=True).distinct()

            # === REAL-TIME NOTIFICATIONS ===

            # 1. Notify the BIDDER (you) that you're now winning
            send_websocket_notification(
                user_id=request.user.id,
                notification_type='bid_confirmed',
                title='أنت الأعلى مزايدة',
                message=f'مزايدتك {bid_amount} د.ع هي الأعلى على {auction.item.title}',
                data={
                    'auction_id': auction.id,
                    'item_id': auction.item.id,
                    'item_title': auction.item.title,
                    'bid_id': bid.id,
                    'your_bid': str(bid_amount),
                    'is_winning': True,
                    'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                    'time_remaining': self._calculate_time_remaining(auction)
                }
            )

            # 2. Notify the item owner about new bid
            send_websocket_notification(
                user_id=auction.item.owner.id,
                notification_type='new_bid',
                title='مزايدة جديدة على مزادك',
                message=f'زايد {request.user.username} بمبلغ {bid_amount} د.ع على {auction.item.title}',
                data={
                    'auction_id': auction.id,
                    'item_id': auction.item.id,
                    'item_title': auction.item.title,
                    'bid_id': bid.id,
                    'bid_amount': str(bid_amount),
                    'previous_price': str(old_price),
                    'bidder_username': request.user.username,
                    'bidder_id': request.user.id,
                    'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                    'time_remaining': self._calculate_time_remaining(auction)
                }
            )

            # 3. Notify the previous winning bidder that they've been outbid
            if previous_winning_bid and previous_winning_bid.bidder != request.user:
                send_websocket_notification(
                    user_id=previous_winning_bid.bidder.id,
                    notification_type='outbid',
                    title='تجاوزك مزايد آخر',
                    message=f'تجاوز مزايد آخر مزايدتك {previous_winning_bid.amount} د.ع على {auction.item.title}',
                    data={
                        'auction_id': auction.id,
                        'item_id': auction.item.id,
                        'item_title': auction.item.title,
                        'your_bid': str(previous_winning_bid.amount),
                        'new_bid': str(bid_amount),
                        'new_bidder': request.user.username,
                        'is_winning': False,
                        'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                        'time_remaining': self._calculate_time_remaining(auction)
                    }
                )

            # 3b. The same two, by push: the socket notices above reach only a
            # screen open on this auction. A live auction has its audience
            # watching already, and a push per bid there is noise.
            if not auction.live_session_id:
                try:
                    from .notifications import notify_new_bid
                    notify_new_bid(auction, bid, previous_winning_bid)
                except Exception as e:
                    logger.error(f"Failed to push new-bid notices for auction {auction.id}: {e}")

            # 4. Notify all other bidders about the new bid
            for bidder_id in previous_bidders:
                if bidder_id != (previous_winning_bid.bidder.id if previous_winning_bid else None):
                    send_websocket_notification(
                        user_id=bidder_id,
                        notification_type='auction_update',
                        title='مزايدة جديدة على مزاد تتابعه',
                        message=f'مزايدة جديدة {bid_amount} د.ع على {auction.item.title}',
                        data={
                            'auction_id': auction.id,
                            'item_id': auction.item.id,
                            'item_title': auction.item.title,
                            'new_bid': str(bid_amount),
                            'bidder': request.user.username,
                            'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                            'time_remaining': self._calculate_time_remaining(auction)
                        }
                    )

            # 5. Broadcast to auction room (for anyone watching the auction page)
            self._broadcast_auction_update(auction, bid, request.user)

            # Return the created bid
            bid_serializer = BidSerializer(bid)
            return Response({
                'success': True,
                'message': f'قُبلت مزايدتك {bid_amount} د.ع وأنت الأعلى الآن',
                'data': bid_serializer.data,
                'auction': {
                    'id': auction.id,
                    'current_price': str(auction.current_price),
                    'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                    'total_bids': auction.bids.count(),
                    'is_winning': True  # NEW: Confirm you're winning
                }
            }, status=status.HTTP_201_CREATED)

        except ValueError as e:
            return Response({
                'success': False,
                'message': 'رصيدك لا يكفي لعربون المزايدة',
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّرت المزايدة')


    def _calculate_time_remaining(self, auction):
        """Calculate time remaining in auction"""
        if auction.end_time:
            remaining = auction.end_time - timezone.now()
            total_seconds = int(remaining.total_seconds())
            if total_seconds > 0:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                return {
                    'total_seconds': total_seconds,
                    'hours': hours,
                    'minutes': minutes,
                    'formatted': f'{hours}h {minutes}m'
                }
        return None

    def _broadcast_auction_update(self, auction, bid, bidder):
        """Broadcast auction update to all connected clients watching this auction"""
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        channel_layer = get_channel_layer()

        # Send to auction-specific room
        async_to_sync(channel_layer.group_send)(
            f'auction_{auction.id}',
            {
                'type': 'auction_update',
                'auction_id': auction.id,
                'event': 'new_bid',
                'data': {
                    'bid_id': bid.id,
                    'bid_amount': str(bid.amount),
                    'bidder_username': bidder.username,
                    'bidder_id': bidder.id,
                    'current_price': str(auction.current_price),
                    'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                    'total_bids': auction.bids.count(),
                    'timestamp': timezone.now().isoformat(),
                    'time_remaining': self._calculate_time_remaining(auction)
                }
            }
        )

        # Also relay to the live-shopping WS channel if this auction is a
        # live-broadcast quick-auction, so viewers watching the stream see
        # bids update without needing a second WebSocket connection.
        if auction.live_session_id:
            broadcast_live_event(auction.live_session_id, 'auction_bid', {
                'auction_id': auction.id,
                'bid_amount': str(bid.amount),
                'bidder_username': bidder.username,
                'current_price': str(auction.current_price),
                'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
                'total_bids': auction.bids.count(),
            })

    @staticmethod
    def _finalize_auction_payments(auction):
        """
        Settle an ended auction (called both from end_early and
        tasks.process_ended_auctions). Must be called inside transaction.atomic().

        Flow:
          1. Refund bid_deposit to every losing bidder who had paid one
             (unchanged — independent of what the winner ends up choosing).
          2. Turn the win into a pending Reservation for the winner instead
             of charging/crediting/transferring ownership immediately — see
             auction_utils.create_winner_reservation. The winner then
             chooses (or is defaulted into, if they don't respond in time)
             full payment now vs. keeping the deposit and paying the rest in
             cash at delivery, same as any other reservation.

        Returns the created Reservation, or None if there were no bids.
        """
        from .auction_utils import (
            create_winner_reservation, refund_losing_deposits, settle_on_winner,
        )

        winning_bid = settle_on_winner(auction)
        if not winning_bid:
            return None  # No bids — nothing to settle

        refund_losing_deposits(auction, winning_bid)
        return create_winner_reservation(auction, winning_bid)

    @swagger_auto_schema(
        operation_description="Get all bids for an auction",
        responses={200: BidSerializer(many=True)}
    )
    @action(detail=True, methods=['get'])
    def bids(self, request, pk=None):
        """Get all bids for an auction"""
        auction = self.get_object()
        bids = auction.bids.all().order_by('-amount')
        serializer = BidSerializer(bids, many=True)

        return Response({
            'success': True,
            'data': serializer.data,
            'total_bids': bids.count(),
            'current_price': str(auction.current_price),
            'minimum_next_bid': str(auction.current_price + auction.minimum_increment)
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Get user's bids for an auction",
        responses={200: BidSerializer(many=True)}
    )
    @action(detail=True, methods=['get'])
    def my_bids(self, request, pk=None):
        """Get current user's bids for an auction"""
        auction = self.get_object()
        bids = auction.bids.filter(bidder=request.user).order_by('-amount')
        serializer = BidSerializer(bids, many=True)

        highest_bid = bids.first() if bids.exists() else None

        return Response({
            'success': True,
            'data': serializer.data,
            'total_bids': bids.count(),
            'highest_bid': str(highest_bid.amount) if highest_bid else None,
            'is_winning': highest_bid.is_winning if highest_bid else False
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Check current user's bidding status in this auction",
        responses={
            200: openapi.Response(
                description="User's bidding status",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'success': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'has_bid': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'is_winning': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'status': openapi.Schema(type=openapi.TYPE_STRING, enum=['winning', 'losing', 'no_bid']),
                        'message': openapi.Schema(type=openapi.TYPE_STRING),
                        'your_highest_bid': openapi.Schema(type=openapi.TYPE_STRING),
                        'current_price': openapi.Schema(type=openapi.TYPE_STRING),
                        'minimum_next_bid': openapi.Schema(type=openapi.TYPE_STRING),
                        'total_bids': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'your_total_bids': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'amount_behind': openapi.Schema(type=openapi.TYPE_STRING),
                        'winning_bidder': openapi.Schema(type=openapi.TYPE_STRING),
                    }
                )
            )
        }
    )
    @action(detail=True, methods=['get'])
    def my_status(self, request, pk=None):
        """Check current user's status in this auction"""
        auction = self.get_object()

        # Get user's highest bid
        user_bid = Bid.objects.filter(
            auction=auction,
            bidder=request.user
        ).order_by('-amount').first()

        # User has not placed any bids
        if not user_bid:
            return Response({
                'success': True,
                'has_bid': False,
                'is_winning': False,
                'status': 'no_bid',
                'message': 'لم تزايد على هذا المزاد بعد',
                'current_price': str(auction.current_price),
                'minimum_bid': str(auction.current_price + auction.minimum_increment),
                'total_bids': auction.bids.count(),
                'auction_status': auction.status,
                'time_remaining': self._calculate_time_remaining(auction)
            }, status=status.HTTP_200_OK)

        # Check if user is winning
        is_winning = user_bid.is_winning

        # Get winning bid for comparison (if user is not winning)
        winning_bid = Bid.objects.filter(
            auction=auction,
            is_winning=True
        ).first()

        # Build response data
        response_data = {
            'success': True,
            'has_bid': True,
            'is_winning': is_winning,
            'your_highest_bid': str(user_bid.amount),
            'your_bid_time': user_bid.created_at.isoformat(),
            'current_price': str(auction.current_price),
            'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
            'total_bids': auction.bids.count(),
            'your_total_bids': auction.bids.filter(bidder=request.user).count(),
            'auction_status': auction.status,
            'time_remaining': self._calculate_time_remaining(auction),
        }

        if is_winning:
            response_data['status'] = 'winning'
            response_data['message'] = f'أنت الأعلى بمزايدة {user_bid.amount} د.ع'
            response_data['lead_amount'] = str(
                user_bid.amount - auction.current_price) if user_bid.amount > auction.current_price else '0'
        else:
            response_data['status'] = 'losing'

            if winning_bid:
                amount_behind = winning_bid.amount - user_bid.amount
                response_data['amount_behind'] = str(amount_behind)
                response_data['winning_bidder'] = winning_bid.bidder.username
                response_data['winning_bid_amount'] = str(winning_bid.amount)
                response_data[
                    'message'] = f'لست الأعلى. أعلى مزايدة {winning_bid.amount} د.ع، وينقصك {amount_behind} د.ع'
            else:
                response_data['message'] = f'لست الأعلى. السعر الحالي {auction.current_price} د.ع'

        return Response(response_data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Cancel an auction (owner only)",
        responses={200: "Auction cancelled successfully"}
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel an auction (only owner can cancel)"""
        auction = self.get_object()

        # Check if user is the item owner
        if auction.item.owner != request.user:
            return Response({
                'success': False,
                'message': 'إلغاء المزاد لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Check if auction has bids
        if auction.bids.exists():
            return Response({
                'success': False,
                'message': 'لا يمكن إلغاء مزاد عليه مزايدات — يمكنك إنهاؤه مبكراً'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check if auction is already ended or completed
        if auction.status in ['ended', 'completed', 'cancelled']:
            return Response({
                'success': False,
                'message': 'المزاد منتهٍ أو ملغى مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                auction.status = 'cancelled'
                auction.save()

                # Update item status back to draft
                auction.item.status = 'draft'
                auction.item.save()

            # Notify all bidders about cancellation
            bidders = auction.bids.values_list('bidder_id', flat=True).distinct()
            for bidder_id in bidders:
                send_websocket_notification(
                    user_id=bidder_id,
                    notification_type='auction_cancelled',
                    title='أُلغي المزاد',
                    message=f'ألغى البائع مزاد {auction.item.title}',
                    data={
                        'auction_id': auction.id,
                        'item_title': auction.item.title
                    }
                )

            return Response({
                'success': True,
                'message': 'أُلغي المزاد'
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر إلغاء المزاد')

    @swagger_auto_schema(
        operation_description="End an auction early (owner only)",
        responses={200: "Auction ended successfully"}
    )
    @action(detail=True, methods=['post'])
    def end_early(self, request, pk=None):
        """End an auction before scheduled end time"""
        auction = self.get_object()

        # Check if user is the item owner
        if auction.item.owner != request.user:
            return Response({
                'success': False,
                'message': 'إنهاء المزاد لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Check if auction is active
        if auction.status != 'active':
            return Response({
                'success': False,
                'message': 'المزاد غير نشط، لا يمكن إنهاؤه'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            reservation = None
            with transaction.atomic():
                # Lock and re-check: the scheduled sweep (tasks.process_ended_auctions)
                # or a double tap may settle this auction concurrently, and
                # settling twice would refund deposits twice.
                auction = AuctionListing.objects.select_for_update().select_related(
                    'item').get(pk=auction.pk)
                if auction.status != 'active':
                    return Response({
                        'success': False,
                        'message': 'المزاد غير نشط، لا يمكن إنهاؤه'
                    }, status=status.HTTP_400_BAD_REQUEST)
                auction.status = 'ended'
                auction.end_time = timezone.now()
                auction.save(update_fields=['status', 'end_time'])

                from .auction_utils import settle_on_winner
                winning_bid = settle_on_winner(auction)
                if winning_bid:
                    # Refund losers + turn the win into a reservation the winner must complete
                    reservation = self._finalize_auction_payments(auction)
                elif auction.item.status != 'archived':
                    auction.item.status = 'draft'
                    auction.item.save(update_fields=['status'])

            # Notify winner
            if auction.winner:
                send_websocket_notification(
                    user_id=auction.winner.id,
                    notification_type='auction_won',
                    title='مبروك! فزت بالمزاد',
                    message=f'فزت بمزاد {auction.item.title} بمبلغ {auction.current_price} د.ع',
                    data={
                        'auction_id': auction.id,
                        'item_id': auction.item.id,
                        'item_title': auction.item.title,
                        'winning_bid': str(auction.current_price),
                        'reservation_id': reservation.id if reservation else None,
                    }
                )
                if reservation:
                    try:
                        notify_auction_won(auction, reservation)
                    except Exception as e:
                        logger.error(f"Failed to send auction-won notification for auction {auction.id}: {e}")

            # Notify all losing bidders
            losing_bidders = auction.bids.exclude(
                bidder=auction.winner
            ).values_list('bidder_id', flat=True).distinct()

            for bidder_id in losing_bidders:
                send_websocket_notification(
                    user_id=bidder_id,
                    notification_type='auction_ended',
                    title='انتهى المزاد',
                    message=f'انتهى مزاد {auction.item.title}',
                    data={
                        'auction_id': auction.id,
                        'item_title': auction.item.title,
                        'final_price': str(auction.current_price),
                        'you_won': False
                    }
                )

            if not auction.live_session_id:
                try:
                    from .notifications import notify_auction_result
                    notify_auction_result(auction)
                except Exception as e:
                    logger.error(f"Failed to push auction result for {auction.id}: {e}")

            from .auction_utils import broadcast_live_auction_ended
            broadcast_live_auction_ended(auction, reservation)

            return Response({
                'success': True,
                'message': 'أُنهي المزاد',
                'winner': UserSerializer(auction.winner).data if auction.winner else None,
                'final_price': str(auction.current_price)
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر إنهاء المزاد')


def _create_quick_live_item(owner, title, description, category_id, condition, quantity, force_live_category=False):
    """
    Creates a minimal backing Item for a live 'quick' feature (quick-show or
    quick-auction) — a product that was never added to the catalog before.
    Falls back to a shared 'Live' category when none is given. Shared by
    LiveSessionViewSet.quick_show and .quick_auction so both stay identical.

    force_live_category always puts the item in the shared 'Live' category,
    even if a category_id was passed — quick_auction sets this so every
    auction started during a broadcast is browsable under 'Live', regardless
    of what the streamer picked.
    """
    if category_id and not force_live_category:
        category = Category.objects.get(id=category_id)
    else:
        category, _ = Category.objects.get_or_create(
            slug='live-quick-show',
            defaults={'name': 'Live', 'is_active': True}
        )

    return Item.objects.create(
        owner=owner,
        title=title,
        description=description or '',
        category=category,
        condition=condition or 'good',
        quantity=quantity,
        status='active',
    )


def _can_moderate_live_session(user, session):
    """True for the session's own store owner, or any user that owner has
    added as a moderator for this specific session (LiveSessionModerator is
    session-scoped — it doesn't carry over to the store's other broadcasts)."""
    store = getattr(user, 'store', None)
    if store is not None and store.id == session.store_id:
        return True
    return session.moderators.filter(user_id=user.id).exists()


class LiveSessionViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin, mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    Live shopping broadcasts (البث المباشر)

    Store owners start a live video broadcast (video/audio itself is
    carried by our own media server — see join_token, it never passes
    through Django), queue catalog items with a live-only price/deposit,
    mark one item "now showing", and viewers reserve it with a single tap
    (deposit charged from their wallet immediately, same as a bid deposit).
    """
    queryset = LiveSession.objects.all().select_related('store', 'store__user')
    # No DestroyModelMixin — a session is ended/cancelled, never deleted
    # (same as AuctionListing). DELETE is still used, but only on
    # sub-resources (queue_item_detail, comment_detail below).
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_class = LiveSessionFilter
    ordering_fields = ['current_viewer_count', 'likes_count', 'total_views', 'started_at', 'created_at']
    ordering = ['-created_at']
    pagination_class = ItemStandardPagination

    def get_permissions(self):
        if self.action in ('queue_items', 'comments') and self.request.method == 'GET':
            return [AllowAny()]
        if self.action in ['create', 'go_live', 'end_session', 'cancel_session',
                            'queue_items', 'queue_item_detail', 'now_showing',
                            'moderators', 'moderator_detail']:
            return [IsStoreOwner()]
        if self.action in ['reserve', 'like', 'unlike', 'my_sessions', 'comments', 'comment_detail',
                            'viewers', 'bans', 'ban_detail', 'join_token',
                            'show_item', 'catalog', 'quick_show', 'quick_auction']:
            return [IsAuthenticated()]
        if self.action in ('list', 'retrieve', 'metadata'):
            return [AllowAny()]
        # Editing, and whatever is added later without a line here: the owner.
        # This used to fall through to AllowAny, and update() was never named,
        # so anyone could retitle or end anybody's stream.
        return [IsStoreOwner()]

    def get_serializer_class(self):
        if self.action == 'list':
            return LiveSessionCardSerializer
        if self.action == 'create':
            return LiveSessionCreateSerializer
        return LiveSessionSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list' and not getattr(self.request.user, 'is_staff', False):
            # A closed store sells nothing, on air included.
            queryset = queryset.filter(open_seller_q('store__user'))
        if self.action == 'retrieve':
            queryset = queryset.prefetch_related('items__item__images')
        return queryset

    def create(self, request, *args, **kwargs):
        """Schedule a new live session, or start one immediately"""
        serializer = LiveSessionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        start_immediately = serializer.validated_data['start_immediately']

        if not request.user.store.is_active:
            return Response({'success': False, 'code': 'store_closed', 'message': STORE_CLOSED},
                            status=status.HTTP_400_BAD_REQUEST)

        if start_immediately:
            existing_live = LiveSession.objects.filter(
                store=request.user.store, status='live'
            ).first()
            if existing_live:
                return Response({
                    'success': False,
                    'message': 'لديك بث جارٍ الآن',
                    'data': LiveSessionSerializer(existing_live, context={'request': request}).data
                }, status=status.HTTP_400_BAD_REQUEST)

        session = LiveSession.objects.create(
            store=request.user.store,
            title=serializer.validated_data['title'],
            description=serializer.validated_data.get('description', ''),
            thumbnail=serializer.validated_data.get('thumbnail'),
            scheduled_at=serializer.validated_data.get('scheduled_at'),
            status='live' if start_immediately else 'scheduled',
            started_at=timezone.now() if start_immediately else None,
        )

        if start_immediately:
            announce_live(session)

        return Response({
            'success': True,
            'message': 'أُنشئ البث',
            'data': LiveSessionSerializer(session, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def my_sessions(self, request):
        """Current user's own live sessions (owner dashboard)"""
        store = getattr(request.user, 'store', None)
        queryset = self.get_queryset().filter(store=store) if store else LiveSession.objects.none()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = LiveSessionSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        serializer = LiveSessionSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def go_live(self, request, pk=None):
        """Transition a scheduled session to live"""
        session = self.get_object()

        if session.status != 'scheduled':
            return Response({
                'success': False,
                'message': 'لا يمكن بدء هذا البث'
            }, status=status.HTTP_400_BAD_REQUEST)
        if not session.store.is_active:
            return Response({'success': False, 'code': 'store_closed', 'message': STORE_CLOSED},
                            status=status.HTTP_400_BAD_REQUEST)

        # Same rule create(start_immediately=True) enforces, and it was
        # missing here: schedule a second session while one is live, then
        # go_live on it, and the store had two. The media is fine (one
        # stream_name each) but everything that asks "is this store live"
        # gets two answers — the live tab shows the store twice, and
        # notify_store_followers_live fires again for the same store.
        if LiveSession.objects.filter(
                store_id=session.store_id, status='live').exists():
            return Response({
                'success': False,
                'message': 'لديك بث جارٍ الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        session.status = 'live'
        session.started_at = timezone.now()
        session.save(update_fields=['status', 'started_at'])

        broadcast_live_event(session.id, 'stream_started', {
            'session_id': session.id,
            'title': session.title,
        })

        announce_live(session)

        return Response({
            'success': True,
            'message': 'أنت الآن في بث مباشر',
            'data': LiveSessionSerializer(session, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='end-session')
    def end_session(self, request, pk=None):
        """End a live session"""
        session = self.get_object()

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'هذا البث ليس جارياً'
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            end_live_session(session, reason='manual')

        return Response({
            'success': True,
            'message': 'انتهى البث',
            'data': LiveSessionSerializer(session, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='cancel-session')
    def cancel_session(self, request, pk=None):
        """Cancel a session before it goes live"""
        session = self.get_object()

        if session.status != 'scheduled':
            return Response({
                'success': False,
                'message': 'يُلغى البث المجدول فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        session.status = 'cancelled'
        session.save(update_fields=['status'])

        return Response({
            'success': True,
            'message': 'أُلغي البث',
            'data': LiveSessionSerializer(session, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Stream credentials — 'publisher' to broadcast (store owner only), 'subscriber' to watch",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'role': openapi.Schema(type=openapi.TYPE_STRING, enum=['subscriber', 'publisher']),
            }
        ),
        responses={200: "Credentials issued", 403: "Only the store owner can publish",
                   503: "Live media is not configured"}
    )
    @action(detail=True, methods=['post'], url_path='join-token')
    def join_token(self, request, pk=None):
        """Credentials to publish (owner) or watch (viewer) this session's stream.

        Media never passes through Django. What comes back is whatever the
        configured provider's clients need — for our SRS box a WHIP or WHEP
        URL carrying a signed token, plus ICE servers for the viewers stuck
        behind carrier NAT. The app reads the envelope's `provider`/`role`
        and ignores fields it does not know, so the next provider change is
        a server-side change only.

        Every credential names the user it was issued to — the media server
        hands that name back on its publish/play hook (core/live_hooks.py),
        which is how a ban is enforced on the stream itself and why
        watching needs an account too.
        """
        session = self.get_object()
        role = request.data.get('role', 'subscriber')

        if role not in ('subscriber', 'publisher'):
            return Response({
                'success': False,
                'message': "role must be 'subscriber' or 'publisher'"
            }, status=status.HTTP_400_BAD_REQUEST)

        if role == 'publisher':
            store = getattr(request.user, 'store', None)
            if not store or store.id != session.store_id:
                return Response({
                    'success': False,
                    'message': 'البث لصاحب المتجر فقط'
                }, status=status.HTTP_403_FORBIDDEN)

        try:
            provider = get_live_media_provider()
            credentials = (provider.publish_credentials if role == 'publisher'
                           else provider.play_credentials)(session, request.user)
        except MediaProviderError as exc:
            # The box is unreachable or a secret is missing — ours to fix,
            # not the caller's. A plain 503, not a 500 traceback.
            logger.error('live media unavailable for session %s: %s', session.id, exc)
            return Response({
                'success': False,
                'code': 'live_media_unavailable',
                'message': 'البث المباشر غير متاح حالياً',
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({'success': True, 'data': credentials}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='queue-items')
    def queue_items(self, request, pk=None):
        """List this session's product queue (GET, public) or add a product to it (POST, owner only)"""
        session = self.get_object()

        if request.method == 'GET':
            items = session.items.select_related('item').prefetch_related('item__images')
            return Response({
                'success': True,
                'data': LiveSessionItemSerializer(items, many=True, context={'request': request}).data
            })

        serializer = LiveSessionItemCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item = Item.objects.get(id=serializer.validated_data['item_id'])
        if item.owner != request.user:
            return Response({
                'success': False,
                'message': 'أضف منتجاتك فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if session.items.filter(item=item, status__in=['queued', 'now_showing']).exists():
            return Response({
                'success': False,
                'message': 'المنتج في القائمة مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        queue_item = LiveSessionItem.objects.create(
            session=session,
            item=item,
            price=serializer.validated_data['price'],
            deposit_amount=serializer.validated_data['deposit_amount'],
            order=serializer.validated_data.get('order', 0),
        )

        return Response({
            'success': True,
            'message': 'أُضيف المنتج إلى القائمة',
            'data': LiveSessionItemSerializer(queue_item, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'],
            url_path='queue-items/(?P<queue_item_id>[^/.]+)')
    def queue_item_detail(self, request, pk=None, queue_item_id=None):
        """Reorder/skip (PATCH) or remove (DELETE, only while still queued) a queue entry"""
        session = self.get_object()

        try:
            queue_item = session.items.get(id=queue_item_id)
        except LiveSessionItem.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود في القائمة'
            }, status=status.HTTP_404_NOT_FOUND)

        if request.method == 'DELETE':
            if queue_item.status != 'queued':
                return Response({
                    'success': False,
                    'message': 'لا يمكن حذف منتج عُرض بالفعل'
                }, status=status.HTTP_400_BAD_REQUEST)
            queue_item.delete()
            return Response({'success': True, 'message': 'حُذف من القائمة'}, status=status.HTTP_200_OK)

        # PATCH — order and/or status in {'queued', 'skipped'} only; the
        # now_showing/sold_out transitions are exclusive to now_showing/reserve.
        new_status = request.data.get('status')
        if new_status is not None and new_status not in ('queued', 'skipped'):
            return Response({
                'success': False,
                'message': "status can only be set to 'queued' or 'skipped' here"
            }, status=status.HTTP_400_BAD_REQUEST)

        update_fields = []
        if 'order' in request.data:
            try:
                order = int(request.data['order'])
            except (TypeError, ValueError):
                order = -1
            if not 0 <= order <= 100000:
                return Response({'success': False, 'message': 'ترتيب غير صالح'},
                                status=status.HTTP_400_BAD_REQUEST)
            queue_item.order = order
            update_fields.append('order')
        if new_status is not None:
            queue_item.status = new_status
            update_fields.append('status')
        if update_fields:
            queue_item.save(update_fields=update_fields)

        return Response({
            'success': True,
            'data': LiveSessionItemSerializer(queue_item, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='now-showing')
    def now_showing(self, request, pk=None):
        """Mark one queued item 'now showing' — enables its Reserve button for viewers"""
        session = self.get_object()

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'البث غير جارٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        queue_item_id = request.data.get('queue_item_id')
        try:
            queue_item = session.items.select_related('item').get(id=queue_item_id, status='queued')
        except LiveSessionItem.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود في القائمة'
            }, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            session.items.filter(status='now_showing').update(status='queued')
            queue_item.status = 'now_showing'
            queue_item.shown_at = timezone.now()
            queue_item.save(update_fields=['status', 'shown_at'])

        broadcast_live_event(session.id, 'product_now_showing', {
            'queue_item_id': queue_item.id,
            'item_id': queue_item.item_id,
            'item_title': queue_item.item.title,
            'price': str(queue_item.price),
            'deposit_amount': str(queue_item.deposit_amount),
            'pay_in_full': get_active_delivery_company() is None,
        })

        return Response({
            'success': True,
            'message': 'تغيّر المنتج المعروض',
            'data': LiveSessionItemSerializer(queue_item, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def catalog(self, request, pk=None):
        """
        The store's own showable items (store owner or a session moderator),
        each pre-annotated with a suggested price/deposit (pulled from its
        FixedPriceListing, if any) and its current state in this session —
        lets the live control screen just be a tap-to-show grid, no manual
        typing needed for an item that's already listed for sale.

        Filtered by the session's store, not request.user — a moderator
        browsing this needs the store owner's catalog, not their own.
        """
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        items = Item.objects.filter(
            owner=session.store.user_id, status__in=['active', 'reserved'], quantity__gt=0
        ).select_related('fixed_listing').prefetch_related('images').order_by('-created_at')

        entries_by_item = {
            entry.item_id: entry
            for entry in session.items.filter(status__in=['queued', 'now_showing', 'sold_out'])
        }

        data = []
        for item in items:
            entry = entries_by_item.get(item.id)
            first_image = item.images.order_by('order').first()
            fixed_listing = getattr(item, 'fixed_listing', None)
            data.append({
                'item_id': item.id,
                'title': item.title,
                'image': request.build_absolute_uri(first_image.image.url) if first_image else None,
                'quantity': item.quantity,
                'suggested_price': str(fixed_listing.price) if fixed_listing else None,
                'suggested_deposit_amount': str(fixed_listing.deposit_amount) if fixed_listing else None,
                'session_status': entry.status if entry else 'not_shown',
                'queue_item_id': entry.id if entry else None,
            })

        return Response({'success': True, 'data': data})

    @swagger_auto_schema(
        operation_description="One-tap 'show this product now' — no separate add-to-queue step. Uses the item's existing listed price/deposit automatically unless overridden.",
        responses={200: LiveSessionItemSerializer, 400: "Item unavailable / has no listed price", 403: "Not your item"}
    )
    @action(detail=True, methods=['post'], url_path='show-item')
    def show_item(self, request, pk=None):
        """
        One-tap alternative to queue-items + now-showing (store owner or a
        session moderator): pick a product from the store's catalog (see
        `catalog` above) and it's live and reservable immediately, price/
        deposit pulled from the item's existing FixedPriceListing so nothing
        needs to be typed.
        """
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'البث غير جارٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        if session.auctions.filter(status='active').exists():
            return Response({
                'success': False,
                'message': 'أنهِ المزاد الجاري قبل عرض منتج آخر'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = ShowLiveItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            item = Item.objects.select_related('fixed_listing').get(
                id=serializer.validated_data['item_id']
            )
        except Item.DoesNotExist:
            return Response({'success': False, 'message': 'المنتج غير موجود'}, status=status.HTTP_404_NOT_FOUND)

        if item.owner_id != session.store.user_id:
            return Response({
                'success': False,
                'message': 'المنتج ليس لهذا المتجر'
            }, status=status.HTTP_403_FORBIDDEN)

        if item.status not in ('active', 'reserved') or item.quantity <= 0:
            return Response({
                'success': False,
                'message': 'المنتج غير متاح للعرض'
            }, status=status.HTTP_400_BAD_REQUEST)

        fixed_listing = getattr(item, 'fixed_listing', None)
        price = serializer.validated_data.get('price') or (fixed_listing.price if fixed_listing else None)
        deposit_amount = serializer.validated_data.get('deposit_amount') or (
            fixed_listing.deposit_amount if fixed_listing else None
        )
        # A moderator shows the store's goods; what they cost is the owner's say.
        is_owner = session.store.user_id == request.user.id
        if not is_owner and (fixed_listing is None or price != fixed_listing.price
                             or deposit_amount != fixed_listing.deposit_amount):
            return Response({
                'success': False,
                'message': 'تغيير السعر في البث لصاحب المتجر وحده'
            }, status=status.HTTP_403_FORBIDDEN)
        if price is not None and deposit_amount is not None and deposit_amount > price:
            return Response({
                'success': False,
                'message': 'لا يتجاوز العربون السعر'
            }, status=status.HTTP_400_BAD_REQUEST)
        if price is None or deposit_amount is None:
            return Response({
                'success': False,
                'message': 'لا سعر لهذا المنتج — اعرضه للبيع أولاً أو اكتب السعر والعربون'
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            queue_item = session.items.select_for_update().filter(
                item=item, status__in=['queued', 'now_showing']).first()
            # A viewer taps 'reserve' on an entry at the price on their screen,
            # and the deposit leaves their wallet at once. A new price is a new
            # entry: a tap still carrying the old one finds it gone.
            if queue_item and queue_item.shown_at and (
                    queue_item.price != price or queue_item.deposit_amount != deposit_amount):
                queue_item.status = 'skipped'
                queue_item.save(update_fields=['status'])
                queue_item = None
            if not queue_item:
                queue_item = LiveSessionItem(session=session, item=item)
            queue_item.price = price
            queue_item.deposit_amount = deposit_amount

            session.items.filter(status='now_showing').exclude(pk=queue_item.pk).update(status='queued')
            queue_item.status = 'now_showing'
            queue_item.shown_at = timezone.now()
            queue_item.save()

        broadcast_live_event(session.id, 'product_now_showing', {
            'queue_item_id': queue_item.id,
            'item_id': item.id,
            'item_title': item.title,
            'price': str(queue_item.price),
            'deposit_amount': str(queue_item.deposit_amount),
            'pay_in_full': get_active_delivery_company() is None,
        })

        return Response({
            'success': True,
            'message': 'معروض الآن',
            'data': LiveSessionItemSerializer(queue_item, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Show a product that was NEVER added to the app before — the seller just types a title/price/deposit while holding it up on camera, no separate 'add product' step. A minimal backing Item is created automatically.",
        responses={201: LiveSessionItemSerializer, 400: "Validation failed"}
    )
    @action(detail=True, methods=['post'], url_path='quick-show')
    def quick_show(self, request, pk=None):
        """
        For a product that doesn't exist in the catalog at all (the common
        spontaneous live-selling case), store owner or a session moderator
        types just a title, price, and deposit right here — a minimal Item
        is created behind the scenes (needed so Reservation/Dispute/wallet
        flows work exactly the same as for a catalog item) and immediately
        marked 'now showing'. This is the one-tap path for "show whatever
        I'm holding, right now" — no catalog browsing or pre-listing
        required. The item is always created under the store owner's
        account (not the moderator posting it) so proceeds/payouts land
        on the right person.
        """
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'البث غير جارٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        if session.auctions.filter(status='active').exists():
            return Response({
                'success': False,
                'message': 'أنهِ المزاد الجاري قبل عرض منتج آخر'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = QuickShowItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        validated = serializer.validated_data

        with transaction.atomic():
            item = _create_quick_live_item(
                owner=session.store.user,
                title=validated['title'],
                description=validated.get('description', ''),
                category_id=validated.get('category_id'),
                condition=validated.get('condition'),
                quantity=validated['quantity'],
            )

            queue_item = LiveSessionItem.objects.create(
                session=session, item=item,
                price=validated['price'], deposit_amount=validated['deposit_amount'],
            )

            session.items.filter(status='now_showing').exclude(pk=queue_item.pk).update(status='queued')
            queue_item.status = 'now_showing'
            queue_item.shown_at = timezone.now()
            queue_item.save(update_fields=['status', 'shown_at'])

        broadcast_live_event(session.id, 'product_now_showing', {
            'queue_item_id': queue_item.id,
            'item_id': item.id,
            'item_title': item.title,
            'price': str(queue_item.price),
            'deposit_amount': str(queue_item.deposit_amount),
            'pay_in_full': get_active_delivery_company() is None,
        })

        return Response({
            'success': True,
            'message': 'معروض الآن',
            'data': LiveSessionItemSerializer(queue_item, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        operation_description="Start a 2-minute live auction — same one-tap spirit as quick-show, but viewers bid instead of instantly reserving. Settles exactly like a regular auction: the winner gets a pending Reservation to complete via /reservations/{id}/complete/.",
        responses={201: LiveAuctionSerializer, 400: "Validation failed / session not live / an auction is already running"}
    )
    @action(detail=True, methods=['post'], url_path='quick-auction')
    def quick_auction(self, request, pk=None):
        """
        Starts a real AuctionListing (bidding/settlement are 100% the
        existing auction machinery — see AuctionViewSet.place_bid and
        tasks.finalize_live_auction) scoped to this live session, running
        for exactly 2 minutes. Only one auction (or one now-showing fixed
        item) is ever in the spotlight at a time — starting an auction
        demotes any fixed item currently showing. Store owner or a session
        moderator; the backing item is always created under the store
        owner's account (see quick_show's docstring for why).
        """
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'البث غير جارٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        if session.auctions.filter(status='active').exists():
            return Response({
                'success': False,
                'message': 'يوجد مزاد جارٍ في هذا البث'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = QuickLiveAuctionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        validated = serializer.validated_data

        with transaction.atomic():
            item = _create_quick_live_item(
                owner=session.store.user,
                title=validated['title'],
                description=validated.get('description', ''),
                category_id=validated.get('category_id'),
                condition=validated.get('condition'),
                quantity=validated['quantity'],
                force_live_category=True,
            )

            now = timezone.now()
            auction = AuctionListing.objects.create(
                item=item,
                starting_price=validated['starting_price'],
                current_price=validated['starting_price'],
                minimum_increment=validated['minimum_increment'],
                bid_deposit=validated['bid_deposit'],
                start_time=now,
                end_time=now + timedelta(minutes=2),
                status='active',
                live_session=session,
            )

            # Only one spotlight at a time — the auction visibly takes over.
            session.items.filter(status='now_showing').update(status='queued')

        from .tasks import finalize_live_auction
        finalize_live_auction.apply_async(args=[auction.id], eta=auction.end_time)

        broadcast_live_event(session.id, 'auction_started', {
            'auction_id': auction.id,
            'item_id': item.id,
            'item_title': item.title,
            'starting_price': str(auction.starting_price),
            'current_price': str(auction.current_price),
            'minimum_increment': str(auction.minimum_increment),
            'minimum_next_bid': str(auction.current_price + auction.minimum_increment),
            'bid_deposit': str(auction.bid_deposit),
            'end_time': auction.end_time.isoformat(),
        })

        return Response({
            'success': True,
            'message': 'بدأ المزاد',
            'data': LiveAuctionSerializer(auction, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get', 'post'], url_path='comments')
    def comments(self, request, pk=None):
        """List recent comments (GET, public) or post a new one (POST, authenticated) — new
        comments broadcast live to everyone in ws/live/<id>/ via the comment_posted event."""
        session = self.get_object()

        if request.method == 'GET':
            limit = _limit(request)
            recent = list(session.comments.select_related('user').order_by('-created_at')[:limit])
            recent.reverse()  # oldest first, chat-feed order
            return Response({
                'success': True,
                'data': LiveSessionCommentSerializer(recent, many=True).data
            })

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'التعليق متاح أثناء البث فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        if session.bans.filter(user_id=request.user.id).exists():
            return Response({
                'success': False,
                'message': 'حُظرت من هذا البث'
            }, status=status.HTTP_403_FORBIDDEN)

        serializer = LiveSessionCommentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        comment = LiveSessionComment.objects.create(
            session=session, user=request.user, content=serializer.validated_data['content']
        )

        broadcast_live_event(session.id, 'comment_posted', LiveSessionCommentSerializer(comment).data)

        return Response({
            'success': True,
            'data': LiveSessionCommentSerializer(comment).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path='comments/(?P<comment_id>[^/.]+)')
    def comment_detail(self, request, pk=None, comment_id=None):
        """Delete a comment — its own author, the store owner, or a session moderator can moderate it"""
        session = self.get_object()
        try:
            comment = session.comments.get(id=comment_id)
        except LiveSessionComment.DoesNotExist:
            return Response({'success': False, 'message': 'التعليق غير موجود'}, status=status.HTTP_404_NOT_FOUND)

        is_author = comment.user_id == request.user.id
        if not (is_author or _can_moderate_live_session(request.user, session)):
            return Response({
                'success': False,
                'message': 'لا يمكنك حذف هذا التعليق'
            }, status=status.HTTP_403_FORBIDDEN)

        comment.delete()
        broadcast_live_event(session.id, 'comment_deleted', {'comment_id': int(comment_id)})

        return Response({'success': True, 'message': 'حُذف التعليق'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'], url_path='viewers')
    def viewers(self, request, pk=None):
        """Paginated, searchable list of signed-in users who have watched this
        session (store owner or a session moderator) — anonymous viewers
        aren't listed here, see LiveSessionViewer's docstring."""
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        queryset = session.viewers.select_related('user').annotate(
            is_moderator=Exists(
                LiveSessionModerator.objects.filter(session_id=session.id, user_id=OuterRef('user_id'))
            ),
            is_banned=Exists(
                LiveSessionBan.objects.filter(session_id=session.id, user_id=OuterRef('user_id'))
            ),
        )

        search = request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(user__username__icontains=search) |
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search)
            )

        active_only = request.query_params.get('active_only', '').lower()
        if active_only in ('true', '1'):
            queryset = queryset.filter(is_watching=True)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = LiveSessionViewerSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        serializer = LiveSessionViewerSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='moderators')
    def moderators(self, request, pk=None):
        """List this session's moderators (GET) or grant moderator powers to
        a user (POST) — store owner only. Moderators can delete comments and
        ban/unban viewers for this specific session (see LiveSessionModerator's
        docstring — the grant doesn't carry over to the store's other broadcasts)."""
        session = self.get_object()

        if request.method == 'GET':
            mods = session.moderators.select_related('user', 'added_by').order_by('-created_at')
            return Response({
                'success': True,
                'data': LiveSessionModeratorSerializer(mods, many=True).data
            })

        serializer = AddLiveSessionModeratorSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        target_user_id = serializer.validated_data['user_id']
        if target_user_id == request.user.id:
            return Response({
                'success': False,
                'message': 'أنت صاحب هذا البث'
            }, status=status.HTTP_400_BAD_REQUEST)

        moderator, created = LiveSessionModerator.objects.get_or_create(
            session=session, user_id=target_user_id, defaults={'added_by': request.user}
        )

        return Response({
            'success': True,
            'message': 'أُضيف المشرف' if created else 'Already a moderator',
            'data': LiveSessionModeratorSerializer(moderator).data
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=['delete'], url_path='moderators/(?P<user_id>[^/.]+)')
    def moderator_detail(self, request, pk=None, user_id=None):
        """Revoke a user's moderator powers for this session — store owner only"""
        session = self.get_object()
        deleted, _ = session.moderators.filter(user_id=user_id).delete()
        if not deleted:
            return Response({'success': False, 'message': 'ليس مشرفاً'}, status=status.HTTP_404_NOT_FOUND)

        return Response({'success': True, 'message': 'أُزيل المشرف'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get', 'post'], url_path='bans')
    def bans(self, request, pk=None):
        """List banned viewers (GET) or ban one (POST) — store owner or a
        session moderator. A ban is scoped to this session only: the target
        can't post comments here and can't reconnect to its WebSocket room;
        it does not affect the store's other broadcasts."""
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        if request.method == 'GET':
            banned = session.bans.select_related('user', 'banned_by').order_by('-created_at')
            return Response({
                'success': True,
                'data': LiveSessionBanSerializer(banned, many=True).data
            })

        serializer = BanLiveSessionViewerSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        target_user_id = serializer.validated_data['user_id']
        if target_user_id == session.store.user_id:
            return Response({
                'success': False,
                'message': 'لا يمكن حظر صاحب المتجر'
            }, status=status.HTTP_400_BAD_REQUEST)
        if target_user_id == request.user.id:
            return Response({
                'success': False,
                'message': 'لا يمكنك حظر نفسك'
            }, status=status.HTTP_400_BAD_REQUEST)

        ban, created = LiveSessionBan.objects.get_or_create(
            session=session, user_id=target_user_id,
            defaults={'banned_by': request.user, 'reason': serializer.validated_data['reason']}
        )

        if created:
            broadcast_live_event(session.id, 'viewer_banned', {'user_id': target_user_id})
            # The picture they are already watching, not only the way back in.
            from .services.live_media import disconnect
            disconnect(session, user_id=target_user_id)

        return Response({
            'success': True,
            'message': 'حُظر المستخدم' if created else 'User already banned',
            'data': LiveSessionBanSerializer(ban).data
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=['delete'], url_path='bans/(?P<user_id>[^/.]+)')
    def ban_detail(self, request, pk=None, user_id=None):
        """Unban a viewer from this session — store owner or a session moderator"""
        session = self.get_object()
        if not _can_moderate_live_session(request.user, session):
            return Response({'success': False, 'message': 'غير مسموح لك بهذا'}, status=status.HTTP_403_FORBIDDEN)

        deleted, _ = session.bans.filter(user_id=user_id).delete()
        if not deleted:
            return Response({'success': False, 'message': 'غير محظور'}, status=status.HTTP_404_NOT_FOUND)

        broadcast_live_event(session.id, 'viewer_unbanned', {'user_id': int(user_id)})
        return Response({'success': True, 'message': 'رُفع الحظر'}, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Reserve the item currently 'now showing' in this session — deposit is charged immediately from the buyer's wallet",
        responses={
            201: ReservationSerializer,
            400: "Not showing right now / insufficient balance / insufficient quantity",
            403: "Cannot reserve your own item",
            404: "Queue item not found"
        }
    )
    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def reserve(self, request, pk=None):
        """
        One-tap reserve of the currently 'now showing' item: locks the queue
        entry then the item (same order as AuctionViewSet.place_bid), charges
        the deposit from the buyer's wallet, decrements stock, and attaches
        the client-captured screenshot as dispute evidence.
        """
        from django.conf import settings

        session = self.get_object()

        if session.status != 'live':
            return Response({
                'success': False,
                'message': 'البث غير جارٍ'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = ReserveLiveItemSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        quantity = serializer.validated_data['quantity']
        sold_out = False

        try:
            with transaction.atomic():
                try:
                    queue_item = LiveSessionItem.objects.select_for_update().select_related('item').get(
                        id=serializer.validated_data['queue_item_id'], session_id=session.id
                    )
                except LiveSessionItem.DoesNotExist:
                    return Response({
                        'success': False,
                        'message': 'المنتج ليس في هذا البث'
                    }, status=status.HTTP_404_NOT_FOUND)

                if queue_item.status != 'now_showing':
                    return Response({
                        'success': False,
                        'message': 'تغيّر المنتج المعروض أو سعره — حدّث وحاول مجدداً'
                    }, status=status.HTTP_400_BAD_REQUEST)

                item = Item.objects.select_for_update().get(id=queue_item.item_id)
                if item.owner == request.user:
                    return Response({
                        'success': False,
                        'message': 'لا يمكنك حجز منتجك'
                    }, status=status.HTTP_403_FORBIDDEN)
                if item.status not in ('active', 'reserved') or not is_open(item.owner):
                    return Response({
                        'success': False,
                        'message': 'لم يعد المنتج معروضاً للبيع'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Live sales go through the item's default variant. The live
                # queue has no picker for colour/size yet, so a live-listed
                # item is treated as its default combination; an optioned item
                # would need a variant on the queue entry before it could be
                # sold live.
                live_variant = item.default_variant
                if live_variant is None:
                    return Response({'success': False, 'message': 'المنتج غير متاح للبيع'},
                                    status=status.HTTP_400_BAD_REQUEST)
                live_variant = ItemVariant.objects.select_for_update().get(pk=live_variant.pk)
                if quantity > live_variant.quantity:
                    return Response({
                        'success': False,
                        'message': f'المتوفّر {live_variant.quantity} فقط'
                    }, status=status.HTTP_400_BAD_REQUEST)

                total_price = queue_item.price * quantity
                if total_price > MAX_AMOUNT:
                    return Response({'success': False, 'message': 'المبلغ أكبر من المسموح — قلّل الكمية'},
                                    status=status.HTTP_400_BAD_REQUEST)
                # A live sale is a deposit now and the rest at the door. With no
                # courier to collect it, the whole price is paid now and the
                # seller hands it over in person, like any other paid order.
                pay_in_full = get_active_delivery_company() is None
                if pay_in_full and not serializer.validated_data['pay_in_full']:
                    return Response({'success': False, 'code': 'update_app',
                                     'message': 'الشراء من البث صار بدفع الثمن كاملاً — حدّث التطبيق لتشتري'},
                                    status=status.HTTP_400_BAD_REQUEST)
                if not pay_in_full and deposit_refused_to(request.user):
                    return Response({'success': False, 'code': 'full_payment_only', 'message': FULL_PAYMENT_ONLY},
                                    status=status.HTTP_400_BAD_REQUEST)
                deposit_amount = queue_item.deposit_amount * quantity

                delivery_location_id = serializer.validated_data.get('delivery_location_id')
                if delivery_location_id:
                    delivery_location = Location.objects.get(id=delivery_location_id, user=request.user, is_active=True)
                else:
                    delivery_location = Location.objects.filter(
                        user=request.user, is_active=True).order_by('-is_primary', '-created_at').first()

                if not delivery_location:
                    return Response({
                        'success': False,
                        'message': 'أضف عنوان توصيل قبل الحجز'
                    }, status=status.HTTP_400_BAD_REQUEST)

                from .services.commission import commission_for
                live_platform_percent, live_platform_amount = commission_for(item, total_price)
                reservation = Reservation.objects.create(
                    item=item,
                    variant=live_variant,
                    platform_commission_percent=live_platform_percent,
                    platform_commission_amount=live_platform_amount,
                    buyer=request.user,
                    quantity=quantity,
                    deposit_amount=deposit_amount,
                    total_price=total_price,
                    status='completed',
                    is_deposit_only=not pay_in_full,
                    completed_at=timezone.now(),
                    warranty_days=item.warranty_days,
                    expires_at=timezone.now() + timedelta(
                        hours=getattr(settings, 'RESERVATION_EXPIRY_HOURS', 24)
                    ),
                    delivery_location=delivery_location,
                    live_session=session,
                    live_session_item=queue_item,
                    proof_screenshot=serializer.validated_data['screenshot'],
                )

                # Debited after the reservation row exists so the Transaction
                # ledger's reservation= links straight to it, matching
                # ReservationViewSet.complete()'s convention. If this raises
                # (insufficient funds), the whole atomic block — including
                # the reservation above — rolls back.
                debit_balance(
                    user=request.user,
                    amount=total_price if pay_in_full else deposit_amount,
                    transaction_type='purchase',
                    item=item,
                    reservation=reservation
                )

                # A live sale is deposit now and the rest at the door, so the
                # trip's fee is collected there too. It was never set, and the
                # courier drove for nothing. Paid in full there is no courier,
                # so no fee.
                from .delivery_utils import compute_delivery_fee
                reservation.delivery_fee = compute_delivery_fee(reservation)
                reservation.save(update_fields=['delivery_fee'])

                live_variant.take_stock(quantity)
                from ledger.services import record_app_sale
                record_app_sale(reservation)
                item.refresh_from_db(fields=['quantity', 'status'])
                item.sold_count += quantity
                item.save(update_fields=['sold_count'])

                if item.quantity == 0:
                    sold_out = True
                    queue_item.status = 'sold_out'
                    queue_item.save(update_fields=['status'])

        except ValueError as e:
            return Response({
                'success': False,
                'message': 'رصيدك لا يكفي لهذا الحجز',
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except (Location.DoesNotExist, TypeError, ValueError):
            return Response({
                'success': False,
                'message': 'عنوان التوصيل غير موجود'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّر الحجز')

        try:
            notify_live_item_reserved(reservation)
        except Exception as e:
            logger.error(f"Failed to send live reservation notification: {e}", exc_info=True)

        broadcast_live_event(session.id, 'item_reserved', {
            'queue_item_id': queue_item.id,
            'item_id': item.id,
            'reservation_id': reservation.id,
            'buyer_username': request.user.username,
            'quantity': quantity,
            'remaining_quantity': item.quantity,
        })

        if sold_out:
            try:
                notify_live_item_sold_out(queue_item)
            except Exception as e:
                logger.error(f"Failed to send sold-out notification: {e}", exc_info=True)
            broadcast_live_event(session.id, 'item_sold_out', {
                'queue_item_id': queue_item.id,
                'item_id': item.id,
            })

        return Response({
            'success': True,
            'message': 'تم الحجز',
            'data': ReservationSerializer(reservation, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """Like this live session"""
        session = self.get_object()
        _, created = LiveSessionLike.objects.get_or_create(session=session, user=request.user)
        if created:
            LiveSession.objects.filter(pk=session.pk).update(likes_count=F('likes_count') + 1)
            session.refresh_from_db(fields=['likes_count'])
            broadcast_live_event(session.id, 'like_update', {'likes_count': session.likes_count})

        return Response({
            'success': True,
            'is_liked': True,
            'likes_count': session.likes_count,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def unlike(self, request, pk=None):
        """Unlike this live session"""
        session = self.get_object()
        deleted, _ = LiveSessionLike.objects.filter(session=session, user=request.user).delete()
        if deleted:
            LiveSession.objects.filter(pk=session.pk, likes_count__gt=0).update(likes_count=F('likes_count') - 1)
            session.refresh_from_db(fields=['likes_count'])
            broadcast_live_event(session.id, 'like_update', {'likes_count': session.likes_count})

        return Response({
            'success': True,
            'is_liked': False,
            'likes_count': session.likes_count,
        }, status=status.HTTP_200_OK)


class ReelViewSet(SafeRankingCacheMixin,
                   mixins.RetrieveModelMixin, mixins.ListModelMixin,
                   mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """
    Reels (الريلز) — short vertical video posted by a store, optionally
    tagging one product. Deliberately no CreateModelMixin: posting a reel is
    a two-step presigned-upload flow (request_upload_url -> client PUTs
    bytes straight to S3 -> finalize), not a single POST with a file field.
    Video/thumbnail bytes never pass through Django — see core/aws_utils.py.
    """
    queryset = Reel.objects.all().select_related('store', 'store__user', 'item')
    filter_backends = [DjangoFilterBackend]
    filterset_class = ReelFilter
    pagination_class = ItemStandardPagination

    def get_permissions(self):
        if self.action in ('request_upload_url', 'finalize', 'my_reels',
                            'update', 'partial_update', 'destroy'):
            return [IsStoreOwner()]
        if self.action == 'comments' and self.request.method == 'GET':
            return [AllowAny()]
        if self.action in ('like', 'unlike', 'comments', 'comment_detail',
                            'save', 'unsave', 'saved'):
            return [IsAuthenticated()]
        return [AllowAny()]

    def get_serializer_class(self):
        if self.action == 'list':
            return ReelCardSerializer
        if self.action == 'request_upload_url':
            return RequestReelUploadSerializer
        if self.action == 'finalize':
            return FinalizeReelSerializer
        return ReelDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        # Discovery (list) only ever shows published reels — pending/failed/
        # archived rows are only reachable through my_reels (owner) or a
        # direct retrieve/finalize by the owning store.
        if not getattr(self.request.user, 'is_staff', False):
            queryset = queryset.filter(store__user__is_active=True)
            # A closed store's reels lead to goods nobody can buy; its owner
            # still sees their own.
            shown = Q(store__is_active=True)
            if self.request.user.is_authenticated:
                shown |= Q(store__user=self.request.user)
            queryset = queryset.filter(shown)
        if self.action == 'list':
            queryset = queryset.filter(status='published')
        elif not getattr(self.request.user, 'is_staff', False):
            # An unpublished reel is its store's alone, by its link as well.
            visible = Q(status='published')
            if self.request.user.is_authenticated:
                visible |= Q(store__user=self.request.user)
            queryset = queryset.filter(visible)
        return self.annotate_viewer_flags(queryset)

    # ---- ترتيب الخلاصة -------------------------------------------------

    # سقف ما يُرتَّب في المرّة الواحدة. الترتيب يجري في بايثون على هذا
    # العدد، فهو ما يحدّ كلفته — ومئتان تكفي لعشر صفحات، وهي أكثر ممّا
    # يسحبه أحد في جلسة.
    @staticmethod
    def _viewer_key(request):
        if request.user.is_authenticated:
            return f'u{request.user.pk}'
        from rest_framework.throttling import BaseThrottle
        return hashlib.sha256((BaseThrottle().get_ident(request) + '|' + request.META.get(
            'HTTP_USER_AGENT', '')[:200]).encode()).hexdigest()[:24]

    FEED_POOL_SIZE = 200
    # ثلاث دقائق: الريلز تُنشر على مدار اليوم، وقائمةٌ محفوظة ساعةً تُخفي
    # ما نُشر للتوّ عن متابعي المتجر.
    FEED_CACHE_TTL = 180
    FEED_TOP_CATEGORIES = 5

    def _ranked_feed_ids(self, user):
        """معرّفات الريلز مرتّبةً لهذا المشاهد، محفوظةً كقائمة أرقام.

        نفس شكل `_recommended_listing_ids` في قوائم السعر الثابت: تُحسب
        مرّة وتُقطَّع صفحاتٍ، فلا يُعاد الترتيب مع كل صفحة.
        """
        from .reel_feed import rank_reels

        authenticated = bool(getattr(user, 'is_authenticated', False))
        cache_key = ('reel_feed_ids:%s' % user.id if authenticated
                     else 'reel_feed_ids:anon')
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        pool = list(
            Reel.objects.filter(status='published', store__user__is_active=True)
            .values_list('id', 'store_id', 'item__category_id', 'likes_count')
            [:self.FEED_POOL_SIZE]
        )

        if not authenticated:
            # الضيف بلا تاريخ: الترتيب الزمني هو كل ما يمكن قوله، ومفتاح
            # واحد مشترك يكفي للجميع.
            ordered = [row[0] for row in pool]
        else:
            seen_ids = set(
                ReelView.objects.filter(user=user).values_list('reel_id', flat=True))
            followed_store_ids = set(
                StoreFollow.objects.filter(follower=user)
                .values_list('store_id', flat=True))
            # متاجر تفاعل معها: إعجاب أو تعليق أو إكمال مشاهدة.
            engaged_store_ids = set(
                Reel.objects.filter(
                    Q(likes__user=user) | Q(comments__user=user)
                    | Q(view_records__user=user, view_records__completed=True)
                ).values_list('store_id', flat=True))
            top_categories = list(
                UserCategoryAffinity.objects.filter(user=user, score__gt=0)
                .order_by('-score')
                .values_list('category_id', flat=True)[:self.FEED_TOP_CATEGORIES])
            category_rank = {cid: i for i, cid in enumerate(top_categories)}

            ordered = rank_reels(
                pool,
                seen_ids=seen_ids,
                followed_store_ids=followed_store_ids,
                engaged_store_ids=engaged_store_ids,
                category_rank=category_rank,
            )

        self._cache_set(cache_key, ordered, self.FEED_CACHE_TTL)
        return ordered

    def list(self, request, *args, **kwargs):
        """الخلاصة مرتّبةً للمشاهد — إلّا حين يُطلب متجر بعينه.

        `?store=<id>` شبكةُ صفحة متجر، وترتيبها الزمني هو المقصود: من يفتح
        متجراً يريد أحدث ما نشره لا ما يناسب ذوقه.
        """
        if request.query_params.get('store') or request.query_params.get('item'):
            return super().list(request, *args, **kwargs)

        ordered_ids = self._ranked_feed_ids(request.user)
        page = self.paginate_queryset(ordered_ids)
        page_ids = page if page is not None else ordered_ids

        by_id = {
            reel.id: reel
            for reel in self.annotate_viewer_flags(
                Reel.objects.filter(id__in=page_ids, status='published', store__user__is_active=True)
                .select_related('store', 'store__user', 'item'))
        }
        ordered_page = [by_id[i] for i in page_ids if i in by_id]

        serializer = ReelCardSerializer(
            ordered_page, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def annotate_viewer_flags(self, queryset):
        """Resolve is_liked_by_me/is_saved_by_me as two correlated EXISTS
        subqueries on the page's own SELECT, instead of the two per-row
        lookups ReelCardSerializer would otherwise issue.

        A page of ItemStandardPagination is 20 reels, so the serializer's
        fallback cost ~40 extra round trips on the feed, on every per-store
        grid (`?store=<id>`) and on `saved/`. EXISTS lets the DB stop at the
        first matching row, and the whole page now costs one query either
        way.

        Anonymous viewers get nothing annotated: they can neither like nor
        save, so the serializer short-circuits to False without a query and
        the subqueries would only be dead weight in the SQL.
        """
        user = self.request.user
        if not user or not user.is_authenticated:
            return queryset
        return queryset.annotate(
            is_liked_by_me=Exists(
                ReelLike.objects.filter(reel_id=OuterRef('pk'), user_id=user.id)
            ),
            is_saved_by_me=Exists(
                SavedReel.objects.filter(reel_id=OuterRef('pk'), user_id=user.id)
            ),
        )

    @swagger_auto_schema(
        operation_description="Request presigned S3 PUT URLs for a new reel's video (and optional thumbnail) — creates a pending_upload Reel immediately",
        request_body=RequestReelUploadSerializer,
        responses={201: "Presigned upload URLs issued", 403: "Store owners only"}
    )
    @action(detail=False, methods=['post'])
    def request_upload_url(self, request):
        """Store owner requests presigned S3 PUT URLs for a new reel's video
        (and optionally thumbnail). Creates the Reel row (status=
        pending_upload) immediately so cleanup_orphaned_reel_uploads has a
        row to find even if the client abandons the flow right after this."""
        from django.conf import settings

        serializer = RequestReelUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors,
            }, status=status.HTTP_400_BAD_REQUEST)

        store = request.user.store
        if Reel.objects.filter(store=store, created_at__gte=timezone.now() - timedelta(days=1)
                               ).count() >= settings.REEL_MAX_UPLOADS_PER_DAY:
            return Response({
                'success': False,
                'message': 'بلغت الحد اليومي لرفع الريلز — حاول غداً'
            }, status=status.HTTP_400_BAD_REQUEST)
        content_type = serializer.validated_data['content_type']
        has_thumbnail = serializer.validated_data['has_thumbnail']

        reel_uuid = uuid.uuid4()
        video_key = f"incoming/{store.id}/{reel_uuid}/video.mp4"
        thumbnail_key = f"incoming/{store.id}/{reel_uuid}/thumb.jpg" if has_thumbnail else ''

        reel = Reel.objects.create(
            store=store,
            video_key=video_key,
            thumbnail_key=thumbnail_key,
            status='pending_upload',
            duration_seconds=0,
        )

        video_upload_url = aws_utils.build_presigned_put_url(
            video_key, content_type, serializer.validated_data.get('content_length'))
        thumbnail_upload_url = (
            aws_utils.build_presigned_put_url(
                thumbnail_key, 'image/jpeg', serializer.validated_data.get('thumbnail_length'))
            if has_thumbnail else None
        )

        return Response({
            'success': True,
            'data': {
                'reel_id': reel.id,
                'video_upload_url': video_upload_url,
                'video_key': video_key,
                'thumbnail_upload_url': thumbnail_upload_url,
                'thumbnail_key': thumbnail_key or None,
                'expires_in': getattr(settings, 'REEL_PRESIGNED_URL_TTL_SECONDS', 900),
            }
        }, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        operation_description="Verify the upload landed in S3, promote it from incoming/ to reels/, and publish the reel",
        request_body=FinalizeReelSerializer,
        responses={200: "Reel published", 400: "Upload missing/invalid", 403: "Not this reel's store"}
    )
    @action(detail=True, methods=['post'])
    def finalize(self, request, pk=None):
        """head_object-verify the incoming upload, copy_object it from
        incoming/ to reels/, delete the incoming copy, and flip the reel to
        published — the only path that takes a Reel out of pending_upload."""
        reel = self.get_object()

        if reel.status != 'pending_upload':
            return Response({
                'success': False,
                'message': 'لا يمكن نشر هذا الريل'
            }, status=status.HTTP_400_BAD_REQUEST)

        video_head = aws_utils.head_object(reel.video_key)
        if not video_head or not video_head['size']:
            return Response({
                'success': False,
                'message': 'لم يكتمل رفع الفيديو'
            }, status=status.HTTP_400_BAD_REQUEST)
        if video_head['size'] > settings.REEL_MAX_VIDEO_BYTES:
            aws_utils.delete_object(reel.video_key)
            return Response({
                'success': False,
                'message': f'الفيديو {settings.REEL_MAX_VIDEO_BYTES // (1024 * 1024)} ميغابايت كحد أقصى'
            }, status=status.HTTP_400_BAD_REQUEST)

        if reel.thumbnail_key:
            thumbnail_head = aws_utils.head_object(reel.thumbnail_key)
            if not thumbnail_head or not thumbnail_head['size']:
                return Response({
                    'success': False,
                    'message': 'لم يكتمل رفع الصورة'
                }, status=status.HTTP_400_BAD_REQUEST)

        serializer = FinalizeReelSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data.get('item_id')
        item = Item.objects.get(id=item_id) if item_id else None

        # Reuse the uuid from the incoming/ key so both prefixes share it.
        reel_uuid = reel.video_key.split('/')[-2]
        new_video_key = f"reels/{reel.store_id}/{reel_uuid}/video.mp4"
        new_thumbnail_key = f"reels/{reel.store_id}/{reel_uuid}/thumb.jpg" if reel.thumbnail_key else ''
        old_video_key, old_thumbnail_key = reel.video_key, reel.thumbnail_key

        with transaction.atomic():
            aws_utils.copy_object(old_video_key, new_video_key)
            if old_thumbnail_key:
                aws_utils.copy_object(old_thumbnail_key, new_thumbnail_key)

            reel.caption = serializer.validated_data.get('caption', '')
            reel.item = item
            reel.duration_seconds = serializer.validated_data['duration_seconds']
            reel.file_size_bytes = video_head['size']
            reel.video_key = new_video_key
            reel.thumbnail_key = new_thumbnail_key
            reel.status = 'published'
            reel.published_at = timezone.now()
            reel.save(update_fields=[
                'caption', 'item', 'duration_seconds', 'file_size_bytes',
                'video_key', 'thumbnail_key', 'status', 'published_at',
            ])

            aws_utils.delete_object(old_video_key)
            if old_thumbnail_key:
                aws_utils.delete_object(old_thumbnail_key)

        from .tasks import notify_store_followers_reel
        notify_store_followers_reel.delay(reel.id)

        return Response({
            'success': True,
            'message': 'نُشر الريل',
            'data': ReelDetailSerializer(reel, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def my_reels(self, request):
        """Current store owner's own reels across all statuses (owner
        dashboard). Serialized with ReelDetailSerializer, not the lighter
        feed card, because this is the screen the owner edits from and the
        edit form needs the current caption.

        'deleted' is excluded: those rows only exist to keep a reported
        reel's moderation trail alive (see destroy) — to the owner the reel
        is gone, and showing it back would read as a failed delete.
        """
        store = getattr(request.user, 'store', None)
        queryset = (
            Reel.objects.filter(store=store).exclude(status='deleted')
            .select_related('store', 'item')
            if store else Reel.objects.none()
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = ReelDetailSerializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)
        serializer = ReelDetailSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def like(self, request, pk=None):
        """Like this reel"""
        reel = self.get_object()
        _, created = ReelLike.objects.get_or_create(reel=reel, user=request.user)
        if created:
            Reel.objects.filter(pk=reel.pk).update(likes_count=F('likes_count') + 1)
            reel.refresh_from_db(fields=['likes_count'])
            # بعد refresh_from_db لا قبله: العتبة تُقرأ من العدّاد المحدَّث،
            # وقراءته قبل التحديث تُفوّت كل عتبة بواحد.
            #
            # ولا يُفشل الإعجابَ فشلُ الإشعار: FCM طرف خارجي قد يتأخر أو
            # يسقط، وضغطة قلب يجب أن تنجح بمعزل عنه.
            try:
                notify_reel_liked(reel, request.user)
            except Exception:
                logger.exception('notify_reel_liked failed for reel %s', reel.pk)

            self._record_reel_interest(request.user, reel, weight=1)

        return Response({
            'success': True,
            'is_liked': True,
            'likes_count': reel.likes_count,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def unlike(self, request, pk=None):
        """Unlike this reel"""
        reel = self.get_object()
        deleted, _ = ReelLike.objects.filter(reel=reel, user=request.user).delete()
        if deleted:
            Reel.objects.filter(pk=reel.pk, likes_count__gt=0).update(likes_count=F('likes_count') - 1)
            reel.refresh_from_db(fields=['likes_count'])

        return Response({
            'success': True,
            'is_liked': False,
            'likes_count': reel.likes_count,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def save(self, request, pk=None):
        """Bookmark this reel into the viewer's favourites (الريلز المحفوظة).

        Private and silent on purpose — the opposite of `like` right above:
        no counter on Reel moves and the store owner is never notified. A
        bookmark is the viewer's own note to come back, not a signal to the
        seller.
        """
        reel = self.get_object()
        SavedReel.objects.get_or_create(reel=reel, user=request.user)
        return Response({
            'success': True,
            'is_saved': True,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def unsave(self, request, pk=None):
        """Remove this reel from the viewer's favourites."""
        reel = self.get_object()
        SavedReel.objects.filter(reel=reel, user=request.user).delete()
        return Response({
            'success': True,
            'is_saved': False,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def saved(self, request):
        """The viewer's saved reels, most recently saved first.

        Ordered by SavedReel.created_at read from the SavedReel table and
        then re-applied in Python, not by `.order_by('saved_by__created_at')`
        on the Reel queryset — the same reasoning as
        FixedPriceListingViewSet.favorites: chaining that order_by after a
        filter on the same reverse relation lets Django join SavedReel twice,
        so a reel saved by several users could be ordered by a *different*
        user's row and appear more than once.

        Unpublished reels are filtered out rather than shown greyed: a reel
        its owner archived or deleted has no video left to play, and a
        bookmark that opens a black screen is worse than one that quietly
        disappears.
        """
        saved_reel_ids = list(
            SavedReel.objects.filter(user=request.user)
            # -id as a tiebreaker, same as Favorite: two saves a moment apart
            # can share a created_at, which would leave their relative order
            # undefined between pages.
            .order_by('-created_at', '-id')
            .values_list('reel_id', flat=True)
        )

        page = self.paginate_queryset(saved_reel_ids)
        page_ids = page if page is not None else saved_reel_ids

        reels_by_id = {
            reel.id: reel
            # Built outside get_queryset (the id__in/ordering dance above),
            # so the viewer-flag annotations have to be applied by hand —
            # without them this page pays the per-row like/save lookups.
            for reel in self.annotate_viewer_flags(
                Reel.objects.filter(id__in=page_ids, status='published', store__user__is_active=True)
                .select_related('store', 'store__user', 'item')
            )
        }
        ordered = [reels_by_id[i] for i in page_ids if i in reels_by_id]

        serializer = ReelCardSerializer(ordered, many=True, context={'request': request})
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='comments')
    def comments(self, request, pk=None):
        """List recent comments with their replies (GET, public) or post a
        comment or a reply (POST, authenticated)"""
        reel = self.get_object()
        ctx = {'store_owner_id': reel.store.user_id if reel.store_id else None}

        if request.method == 'GET':
            limit = _limit(request)
            # الحدّ على التعليقات الرئيسية وحدها: ردود تعليقٍ مقطوعة من
            # منتصفها تُبقي سؤالاً بلا جوابه الظاهر.
            recent = list(
                reel.comments.filter(parent__isnull=True)
                .select_related('user')
                .prefetch_related(Prefetch(
                    'replies',
                    queryset=ReelComment.objects.select_related('user', 'reply_to')
                    .order_by('created_at', 'id'),
                    to_attr='thread_replies',
                ))
                .order_by('-created_at')[:limit]
            )
            recent.reverse()  # oldest first, chat-feed order
            return Response({
                'success': True,
                'data': ReelCommentThreadSerializer(recent, many=True, context=ctx).data
            })

        serializer = ReelCommentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        parent = None
        reply_to = None
        parent_id = serializer.validated_data.get('parent_id')
        if parent_id is not None:
            target = reel.comments.select_related('user').filter(id=parent_id).first()
            if target is None:
                return Response({
                    'success': False,
                    'message': 'التعليق الذي تردّ عليه لم يعد موجوداً',
                }, status=status.HTTP_404_NOT_FOUND)
            # ردّ على ردّ: يُعلَّق تحت التعليق الرئيسي، ويُسمّى من رُدّ عليه.
            if target.parent_id:
                parent, reply_to = target.parent, target.user
            else:
                parent = target

        comment = ReelComment.objects.create(
            reel=reel, user=request.user, parent=parent, reply_to=reply_to,
            content=serializer.validated_data['content'],
        )
        Reel.objects.filter(pk=reel.pk).update(comments_count=F('comments_count') + 1)

        try:
            notify_reel_comment(comment)
        except Exception:
            logger.exception('notify_reel_comment failed for comment %s', comment.pk)

        # أثقل من الإعجاب: التعليق يكلّف المستخدم كتابةً، فلا يُكتب إلا
        # عن اهتمام حقيقي.
        self._record_reel_interest(request.user, reel, weight=2)

        return Response({
            'success': True,
            'data': ReelCommentSerializer(comment, context=ctx).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path='comments/(?P<comment_id>[^/.]+)')
    def comment_detail(self, request, pk=None, comment_id=None):
        """Delete a comment — its own author or the reel's store owner can moderate it"""
        reel = self.get_object()
        try:
            comment = reel.comments.get(id=comment_id)
        except ReelComment.DoesNotExist:
            return Response({'success': False, 'message': 'التعليق غير موجود'}, status=status.HTTP_404_NOT_FOUND)

        is_author = comment.user_id == request.user.id
        is_store_owner = reel.store.user_id == request.user.id
        if not (is_author or is_store_owner):
            return Response({
                'success': False,
                'message': 'لا يمكنك حذف هذا التعليق'
            }, status=status.HTTP_403_FORBIDDEN)

        # حذف تعليق رئيسي يحذف ردوده معه (CASCADE)، فالعدّاد ينقص بها كلها.
        removed = 1 + comment.replies.count()
        comment.delete()
        Reel.objects.filter(pk=reel.pk).update(
            comments_count=Greatest(F('comments_count') - removed, 0))

        return Response({
            'success': True, 'message': 'حُذف التعليق', 'removed': removed,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def view(self, request, pk=None):
        """Record a view — bumps the counter for anonymous and signed-in
        viewers alike; signed-in users additionally get a ReelView upserted so
        repeat watches can be told apart from first watches.

        The reel's own store owner is the one exception: they aren't audience.
        Without this, an owner scrolling past their own reel in the feed — or
        replaying it from my_reels — inflates the very number they use to
        judge how it performed, and can inflate it at will. `counted` says
        which happened, so a client can tell a skipped view from a failed one.
        """
        reel = self.get_object()

        is_owner = request.user.is_authenticated and reel.store.user_id == request.user.id
        if is_owner:
            return Response({
                'success': True,
                'view_count': reel.view_count,
                'counted': False,
            }, status=status.HTTP_200_OK)

        # Once per person (or device) in a while: a loop of taps is one viewer.
        if cache.add(f'reel-view:{reel.pk}:{self._viewer_key(request)}', 1, 6 * 3600):
            Reel.objects.filter(pk=reel.pk).update(view_count=F('view_count') + 1)
            reel.refresh_from_db(fields=['view_count'])

        # بلغ المشاهد نهاية الريل. أقوى إشارة اهتمام وأرخصها: الإعجاب
        # والتعليق يفعلهما قلّة، وإكمال المشاهدة يفعله كل من أعجبه فعلاً
        # بلا أن يُطلب منه شيء.
        completed = bool(request.data.get('completed'))

        if request.user.is_authenticated:
            view_record, created = ReelView.objects.get_or_create(
                reel=reel, user=request.user,
                defaults={'completed': completed},
            )
            if not created:
                updates = {'view_count': F('view_count') + 1}
                # يُرفع ولا يُخفض: من أكمل مرّة أكملها، ومرورٌ سريع لاحقاً
                # لا يمحو ذلك.
                if completed and not view_record.completed:
                    updates['completed'] = True
                ReelView.objects.filter(pk=view_record.pk).update(**updates)

            if completed:
                self._record_reel_interest(request.user, reel, weight=1)

        return Response({
            'success': True,
            'view_count': reel.view_count,
            'counted': True,
        }, status=status.HTTP_200_OK)

    @staticmethod
    def _record_reel_interest(user, reel, weight):
        """يُغذّي UserCategoryAffinity من تفاعل الريلز.

        نفس الجدول الذي تستعمله توصيات المنتجات — لا نظام ثانٍ بجانبه:
        من يشاهد ريلزات الهواتف حتى آخرها يريد أن يرى هواتف في المتجر
        أيضاً، والعكس صحيح.

        وأوزانها أخفّ من أوزان المنتجات (متابعة=1، تفضيل=2، شراء=3):
        إعجابٌ بريل ليس شراءً.
        """
        category = getattr(getattr(reel, 'item', None), 'category', None)
        if category is not None:
            record_interest(user, category, weight=weight)

    def destroy(self, request, *args, **kwargs):
        """Permanent delete at the owner's request — the row AND the S3
        objects. Deliberately not the archive_reel() soft-delete the rest of
        this codebase defaults to: an owner deleting their own reel means the
        video should stop existing on Amazon, not just drop out of the feed.
        (archive_reel stays the path for *moderation* — see resolve_report.)

        The one exception is a reel someone has reported. Report.reported_reel
        is CASCADE, so dropping the row would take the moderation trail with
        it and hand any store an easy way to erase evidence against itself.
        Those keep a tombstone row instead — status='deleted', S3 keys blanked
        and the objects purged all the same — so nothing is playable anywhere
        while the report stays auditable. The owner sees no difference: it's
        gone from my_reels either way.

        S3 is deleted after the DB write, not before, because S3 isn't
        transactional. Of the two ways they can disagree, an orphaned S3
        object is recoverable garbage while a live row pointing at deleted
        media is a permanently broken reel.
        """
        reel = self.get_object()
        reel_id = reel.id
        video_key, thumbnail_key = reel.video_key, reel.thumbnail_key

        if reel.reports.exists():
            reel.status = 'deleted'
            reel.video_key = ''
            reel.thumbnail_key = ''
            reel.save(update_fields=['status', 'video_key', 'thumbnail_key'])
        else:
            reel.delete()

        for key in (video_key, thumbnail_key):
            if not key:
                continue
            try:
                aws_utils.delete_object(key)
            except Exception:
                # The row is already gone; a failed S3 delete leaves an
                # unreachable object, not a broken reel. Log it for the
                # bucket audit rather than failing the owner's request.
                logger.exception('Failed to delete S3 object %s for reel %s', key, reel_id)

        return Response({
            'success': True,
            'message': 'تم حذف الريل نهائياً',
            'data': None,
        })


def _mark_reservation_received(reservation):
    """
    Shared by ReservationViewSet.approve_and_release_payment and
    DeliveryOrderViewSet.confirm_receipt — both are "the buyer pressed
    received" and must settle the sale identically.
    """
    reservation.buyer_approved = True
    reservation.buyer_approved_at = timezone.now()
    reservation.status = 'confirmed'
    reservation.save(update_fields=['buyer_approved', 'buyer_approved_at', 'status'])
    release_reservation_payment(reservation)
    # The seller hears their money arrived (the email this task also sends
    # is off on this platform; the push is what reaches them).
    from .tasks import send_payment_release_notification
    transaction.on_commit(
        lambda: send_payment_release_notification.delay(reservation.id, 'buyer_approved'))


class AddressNotChangeable(Exception):
    """The new address costs a different fee that cannot be charged here."""


def _reprice_delivery(reservation):
    """The buyer moved a paid order before it was sent: what the trip to the new
    address costs is what they pay. Prepaid, the difference moves now; on cash
    it is in what the courier will collect. Raises ValueError when the wallet
    cannot cover it. Caller holds the reservation's lock."""
    from .delivery_utils import compute_delivery_fee
    from .services.installments.service import live_plan_for

    if reservation.status != 'completed':
        # Not paid yet: the fee is computed at payment. But a request already
        # priced at the financier must not move under them.
        if reservation.installment_plans.filter(state__in=['requested', 'confirming']).exists():
            raise AddressNotChangeable('لديك طلب تقسيط قيد المراجعة على هذا العنوان — ألغِه أولاً')
        return
    fee = compute_delivery_fee(reservation)
    difference = fee - reservation.delivery_fee
    if difference == 0:
        return
    if not reservation.is_deposit_only:
        if live_plan_for(reservation) is not None:
            raise AddressNotChangeable(
                'أجرة التوصيل إلى هذا العنوان مختلفة، وطلبك مقسّط — اختر عنواناً في المحافظة نفسها')
        if difference > 0:
            debit_balance(reservation.buyer, difference, 'delivery_fee',
                          item=reservation.item, reservation=reservation)
        else:
            refund_to_buyer(reservation, -difference, reason='فرق أجرة التوصيل')
    reservation.delivery_fee = fee


def _resolve_variant(item, variant_id):
    """Which combination a reservation is for.

    Returns (variant, None) or (None, error message). Rules:
      - item has no options -> its default variant, whatever the client sent
        (so a client that never learned about variants keeps working);
      - item has options and no variant_id -> error, never a silent guess at
        which colour the buyer meant;
      - variant_id must belong to this item and still be active.
    """
    if not item.options:
        variant = item.default_variant
        return (variant, None) if variant else (None, 'هذا المنتج غير متاح للبيع حالياً')
    if not variant_id:
        return None, 'اختر اللون/المقاس أولاً'
    variant = ItemVariant.objects.select_for_update().filter(
        pk=variant_id, item=item, is_active=True).first()
    if variant is None:
        return None, 'الخيار المحدّد غير متوفّر'
    return variant, None


class ReservationViewSet(mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin, mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """
    Manage reservations for regular items and resale items

    Enhanced to handle:
    - Items being resold through transfers
    - Automatic commission calculation
    - Payment distribution to original owner and reseller
    """
    # No DestroyModelMixin — a reservation is cancelled (see the `cancel`
    # action below), never deleted: hard-deleting it would CASCADE-wipe any
    # linked Dispute/DeliveryOrder rows (see Dispute.reservation and
    # DeliveryOrder.reservation), destroying financial/legal history. Same
    # pattern as LiveSessionViewSet above.
    queryset = Reservation.objects.all()
    serializer_class = ReservationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Reservation.objects.none()

        # installment_plans is prefetched for the `installment` field on the
        # serializer — without it the list grows one query per row, and the
        # orders screen is the most-opened screen in the app.
        base = Reservation.objects.prefetch_related('installment_plans__collections')
        if self.action == 'list':
            base = reservation_related(base)

        if self.request.user.is_staff or self.request.user.is_superuser:
            return base.all().order_by('-reserved_at')

        from django.db.models import Q
        return base.filter(
            Q(buyer=self.request.user) | Q(item__owner=self.request.user)
        ).order_by('-reserved_at')

    @swagger_auto_schema(
        operation_description="""
        Create a reservation for an item (works for both regular and resale items)

        For resale items:
        - Automatically detects if item is from a transfer
        - Calculates commission for original owner
        - Links reservation to transfer
        """,
        request_body=CreateReservationSerializer,
        responses={
            201: ReservationSerializer,
            400: "Validation failed or item unavailable",
            403: "Permission denied",
            404: "Item or listing not found"
        }
    )
    @action(detail=False, methods=['get'], url_path='delivery-quote')
    def delivery_quote(self, request):
        """What delivery will cost for `item_id` to `location_id` — so the
        reservation sheet can show the fee before the buyer commits. Same
        rule as the charge itself (delivery_utils.quote_delivery_fee)."""
        from .delivery_utils import get_active_delivery_company, quote_delivery_fee, resolve_pickup
        try:
            item = Item.objects.select_related('owner', 'current_holder').get(
                id=int(request.query_params.get('item_id', 0)))
            location = Location.objects.get(
                id=int(request.query_params.get('location_id', 0)), user=request.user)
        except (Item.DoesNotExist, Location.DoesNotExist, ValueError):
            return Response({'success': False, 'message': 'المنتج أو العنوان غير موجود'},
                            status=status.HTTP_404_NOT_FOUND)
        company = get_active_delivery_company()
        if company is None:
            return Response({'success': True, 'data': {'fee': '0.00', 'company': None}})
        seller_location, _, _ = resolve_pickup(item)
        fee = quote_delivery_fee(company, seller_location.state if seller_location else '',
                                 location.state, item.package_size)
        return Response({'success': True, 'data': {
            'fee': str(fee), 'company': company.name,
            'from_governorate': seller_location.state if seller_location else None,
            'to_governorate': location.state, 'package_size': item.package_size,
        }})

    @action(detail=False, methods=['post'])
    def create_reservation(self, request):
        """Create a new reservation (handles both regular and resale items)"""
        serializer = CreateReservationSerializer(
            data=request.data,
            context={'request': request}
        )

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data['item_id']
        quantity = serializer.validated_data.get('quantity', 1)
        delivery_location_id = serializer.validated_data.get('delivery_location_id')
        coupon_collection_id = serializer.validated_data.get('coupon_collection_id')
        offer_message_id = serializer.validated_data.get('offer_message_id')

        if coupon_collection_id and offer_message_id:
            return Response({
                'success': False,
                'message': 'لا يمكن استخدام كوبون وعرض مقبول في نفس الحجز'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            item = Item.objects.get(id=item_id)
        except Item.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)

        # Check if user is trying to buy their own item
        if item.owner == request.user:
            return Response({
                'success': False,
                'message': 'لا يمكنك حجز منتجك'
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            with transaction.atomic():
                item = Item.objects.select_for_update().get(id=item_id)

                try:
                    fixed_listing = item.fixed_listing
                except FixedPriceListing.DoesNotExist:
                    return Response({
                        'success': False,
                        'message': 'المنتج غير معروض بسعر ثابت'
                    }, status=status.HTTP_404_NOT_FOUND)

                if item.status not in ['active', 'reserved']:
                    return Response({
                        'success': False,
                        'message': 'المنتج غير متاح'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Stock is checked on the combination being bought, not on the
                # item's total: 20 in stock across four colours says nothing about
                # whether red / L has any. A plain item resolves to its default
                # variant so older clients that send no variant_id still work.
                variant, variant_error = _resolve_variant(
                    item, serializer.validated_data.get('variant_id'))
                if variant_error:
                    return Response({'success': False, 'message': variant_error},
                                    status=status.HTTP_400_BAD_REQUEST)
                if quantity > variant.quantity:
                    return Response({
                        'success': False,
                        'message': f'المتوفّر {variant.quantity} فقط'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # A variant can carry its own price; the buyer pays for the
                # combination they chose.
                unit_price = variant.price if variant.price is not None else fixed_listing.price

                # Redeem a collected store coupon (كوبون المتجر), if provided
                coupon_collection = None
                coupon_discount = Decimal('0.00')
                if coupon_collection_id:
                    try:
                        coupon_collection = CouponCollection.objects.select_for_update().select_related(
                            'coupon', 'coupon__store'
                        ).get(id=coupon_collection_id, user=request.user)
                    except CouponCollection.DoesNotExist:
                        return Response({
                            'success': False,
                            'message': 'الكوبون غير موجود في محفظتك'
                        }, status=status.HTTP_404_NOT_FOUND)

                    if hasattr(coupon_collection, 'used_in_reservation'):
                        return Response({
                            'success': False,
                            'message': 'تم استخدام هذا الكوبون مسبقاً'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if not coupon_collection.coupon.is_valid:
                        return Response({
                            'success': False,
                            'message': 'انتهت صلاحية الكوبون'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if coupon_collection.coupon.store.user_id != item.owner_id:
                        return Response({
                            'success': False,
                            'message': 'هذا الكوبون لا يخص متجر هذا المنتج'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if (unit_price * quantity) < coupon_collection.coupon.min_order_amount:
                        return Response({
                            'success': False,
                            'message': f'الحد الأدنى لاستخدام هذا الكوبون هو {coupon_collection.coupon.min_order_amount} IQD'
                        }, status=status.HTTP_400_BAD_REQUEST)

                # Redeem an accepted chat offer (عرض مقبول) as this
                # reservation's negotiated price, if provided
                offer_message = None
                if offer_message_id:
                    try:
                        offer_message = Message.objects.select_for_update().select_related(
                            'chat'
                        ).get(id=offer_message_id)
                    except Message.DoesNotExist:
                        return Response({
                            'success': False,
                            'message': 'العرض غير موجود'
                        }, status=status.HTTP_404_NOT_FOUND)

                    if offer_message.message_type not in ('offer', 'counter_offer'):
                        return Response({
                            'success': False,
                            'message': 'هذه ليست رسالة عرض سعر'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if offer_message.offer_status != 'accepted':
                        return Response({
                            'success': False,
                            'message': 'هذا العرض لم يُقبل بعد من البائع'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    # A price agreed for now, not for good.
                    agreed = offer_message.offer_accepted_at or offer_message.created_at
                    if timezone.now() > agreed + timedelta(hours=settings.OFFER_VALID_HOURS):
                        return Response({
                            'success': False,
                            'message': f'انتهت مدة العرض ({settings.OFFER_VALID_HOURS} ساعة من قبوله) — أرسل عرضاً جديداً'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if offer_message.chat.item_id != item.id:
                        return Response({
                            'success': False,
                            'message': 'هذا العرض لا يخص هذا المنتج'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if offer_message.chat.buyer_id != request.user.id:
                        return Response({
                            'success': False,
                            'message': 'هذا العرض ليس لك'
                        }, status=status.HTTP_403_FORBIDDEN)

                    if hasattr(offer_message, 'redeemed_in_reservation'):
                        return Response({
                            'success': False,
                            'message': 'تم استخدام هذا العرض مسبقاً'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    # The seller said yes to a price for one combination.
                    if item.options and offer_message.offer_variant_id != variant.pk:
                        return Response({
                            'success': False,
                            'message': 'هذا العرض لخيار آخر من المنتج — قدّم عرضاً على الخيار الذي تريده'
                        }, status=status.HTTP_400_BAD_REQUEST)

                    if quantity != offer_message.offer_quantity:
                        return Response({
                            'success': False,
                            'message': f'هذا العرض مقدَّم لكمية {offer_message.offer_quantity} فقط'
                        }, status=status.HTTP_400_BAD_REQUEST)

                expiry_time = timezone.now() + timedelta(
                    hours=getattr(settings, 'RESERVATION_EXPIRY_HOURS', 24)
                )

                total_price = unit_price * quantity
                if total_price > MAX_AMOUNT:
                    return Response({'success': False, 'message': 'المبلغ أكبر من المسموح — قلّل الكمية'},
                                    status=status.HTTP_400_BAD_REQUEST)
                # The listing's stored deposit is per unit *at the listing
                # price*; a variant with its own price needs the percentage
                # applied to that price, or a 150-IQD variant would carry a
                # 12,000-IQD deposit.
                if variant.price is not None:
                    deposit_amount = (
                        unit_price * fixed_listing.deposit_percentage / Decimal('100')
                    ) * quantity
                else:
                    deposit_amount = fixed_listing.deposit_amount * quantity

                if coupon_collection:
                    # A coupon is a discount, not a gift: one that left a dinar
                    # to pay made a confirmed sale and a review for no commission.
                    coupon_discount = min(
                        coupon_collection.coupon.amount,
                        (total_price * settings.COUPON_MAX_PERCENT / 100).quantize(Decimal('0.01')))
                    total_price -= coupon_discount
                    # The deposit was sized on the undiscounted price; a big
                    # coupon could leave it larger than the whole order, which
                    # turned the cash-on-delivery remainder negative.
                    deposit_amount = min(deposit_amount, total_price)

                if offer_message:
                    # The negotiated price replaces the listing price entirely
                    # (already validated: quantity == offer_message.offer_quantity)
                    total_price = offer_message.offer_amount
                    deposit_amount = (
                        fixed_listing.deposit_percentage / Decimal('100')
                    ) * total_price

                # Get delivery location if provided
                delivery_location = None
                if delivery_location_id:
                    from .models import Location
                    try:
                        delivery_location = Location.objects.get(
                            id=delivery_location_id,
                            user=request.user, is_active=True
                        )
                    except (Location.DoesNotExist, TypeError, ValueError):
                        pass

                # Create reservation — this only holds a spot and records the
                # delivery location; stock isn't touched until the buyer
                # actually completes the reservation (pays deposit or full
                # price), so multiple reservations can coexist on one item.
                from .services.commission import commission_for
                commission_percent, commission_amount_platform = commission_for(item, total_price)
                reservation = Reservation.objects.create(
                    item=item,
                    variant=variant,
                    buyer=request.user,
                    quantity=quantity,
                    deposit_amount=deposit_amount,
                    total_price=total_price,
                    expires_at=expiry_time,
                    delivery_location=delivery_location,
                    applied_coupon=coupon_collection,
                    coupon_discount=coupon_discount,
                    applied_offer=offer_message,
                    warranty_days=item.warranty_days,
                    platform_commission_percent=commission_percent,
                    platform_commission_amount=commission_amount_platform,
                )

            # Send notifications to seller and buyer
            try:
                notify_reservation_created(reservation)
                # تذكير المشتري بعد ساعة بإكمال حجزه. countdown لا beat:
                # الساعة تُحسب من لحظة هذا الحجز بالذات، ومسح دوري كان
                # سيُذكّر من حجز قبل خمس دقائق.
                remind_to_complete_reservation.apply_async(
                    args=[reservation.id], countdown=3600)
                seller = reservation.item.owner
                send_websocket_notification(
                    user_id=seller.id,
                    notification_type='reservation_created',
                    title='حجز جديد',
                    message=f'حجز {reservation.buyer.username} منتجك "{reservation.item.title}".',
                    data={'reservation_id': reservation.id, 'item_title': reservation.item.title}
                )
            except Exception as e:
                logger.error(f"Failed to send reservation notification: {e}", exc_info=True)

            # Build response message
            message = f'تم حجز {quantity} قطعة'

            return Response({
                'success': True,
                'message': message,
                'data': ReservationSerializer(reservation, context={'request': request}).data
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر إنشاء الحجز')

    @swagger_auto_schema(
        operation_description="""
        Complete reservation and handle payment distribution for resale items

        Pass `payment_type` as `"deposit"` to pay only the deposit now (the
        remainder is collected in cash at delivery, outside the app), or
        `"full"` (default) to pay the entire price now.

        If the reservation has no delivery address yet (e.g. an auction win,
        whose Reservation is created automatically with no address on file),
        pass `delivery_location_id` to attach one of the buyer's saved
        locations in the same call. Required in that case — otherwise this
        returns 400.
        """
    )
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Complete a reservation (handles payment distribution for resale items)"""
        reservation = self.get_object()

        if reservation.buyer != request.user:
            return Response({
                'success': False,
                'message': 'إكمال الحجز للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if reservation.status != 'pending':
            return Response({
                'success': False,
                'message': 'لا يمكن إكمال هذا الحجز'
            }, status=status.HTTP_400_BAD_REQUEST)

        if timezone.now() > reservation.expires_at:
            return Response({
                'success': False,
                'message': 'انتهت مدة الحجز'
            }, status=status.HTTP_400_BAD_REQUEST)

        # An open installment request means the admin may be activating the
        # plan at Qi right now. Paying from the wallet as well would leave a
        # live salary deduction behind an order that is already paid.
        if reservation.installment_plans.filter(state='requested').exists():
            return Response({
                'success': False,
                'code': 'installment_requested',
                'message': 'لديك طلب تقسيط قيد المراجعة لهذا الحجز — ألغِه أولاً '
                           'إن أردت الدفع بطريقة أخرى'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not reservation.delivery_location_id:
            delivery_location_id = request.data.get('delivery_location_id')
            if not delivery_location_id:
                return Response({
                    'success': False,
                    'message': 'أضف عنوان توصيل قبل إكمال الحجز'
                }, status=status.HTTP_400_BAD_REQUEST)
            try:
                reservation.delivery_location = Location.objects.get(
                    id=delivery_location_id, user=request.user, is_active=True
                )
            except (Location.DoesNotExist, TypeError, ValueError):
                return Response({
                    'success': False,
                    'message': 'عنوان التوصيل غير موجود'
                }, status=status.HTTP_400_BAD_REQUEST)
            reservation.save(update_fields=['delivery_location'])

        payment_type = request.data.get('payment_type', 'full')
        if payment_type not in ('full', 'deposit'):
            return Response({
                'success': False,
                'message': 'payment_type must be "full" or "deposit"'
            }, status=status.HTTP_400_BAD_REQUEST)

        # A deposit order is finished by the courier who collects the rest; with
        # none active it could only ever be cancelled. An auction win too: its
        # bid deposit is paid, but the rest has nobody to collect it either.
        if payment_type == 'deposit' and not get_active_delivery_company():
            return Response({
                'success': False,
                'code': 'full_payment_only',
                'message': NO_COURIER
            }, status=status.HTTP_400_BAD_REQUEST)

        if payment_type == 'deposit' and not reservation.auction_id and deposit_refused_to(request.user):
            return Response({
                'success': False,
                'code': 'full_payment_only',
                'message': FULL_PAYMENT_ONLY
            }, status=status.HTTP_400_BAD_REQUEST)

        # A 0% deposit is a deliberate seller choice — "don't take any
        # deposit at all", not "force full payment". 'deposit' with
        # deposit_amount=0 is a valid, meaningful choice here: it charges
        # nothing in-app now and defers the entire price to cash-on-delivery
        # (see complete_reservation/create_outbound_delivery_order).

        try:
            with transaction.atomic():
                complete_reservation(reservation, payment_type)

            # Completed purchase is the strongest interest signal available —
            # reinforce the buyer's affinity for this item's category.
            record_interest(reservation.buyer, reservation.item.category, weight=3)

            # Notify seller that buyer completed the reservation
            try:
                notify_reservation_completed(reservation)
                seller = reservation.item.owner
                send_websocket_notification(
                    user_id=seller.id,
                    notification_type='reservation_completed',
                    title='تم اكمال الحجز',
                    message=f'{reservation.buyer.username} أكمل الحجز لمنتجك "{reservation.item.title}" وتم خصم المبلغ.',
                    data={'reservation_id': reservation.id, 'item_title': reservation.item.title}
                )
            except Exception as e:
                logger.error(f"Failed to send reservation completed notification: {e}", exc_info=True)

            message = 'اكتمل الحجز'

            return Response({
                'success': True,
                'message': message,
                'data': ReservationSerializer(reservation, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except ItemWithdrawnError as e:
            return Response({
                'success': False,
                'code': 'item_withdrawn',
                'message': 'لم يعد المنتج معروضاً للبيع، ولم يُخصم منك شيء',
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except OutOfStockError as e:
            return Response({
                'success': False,
                'code': 'out_of_stock',
                'message': e.for_buyer,
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e:
            # رمز ثابت يقرأه التطبيق ليحوّل المشتري إلى شحن محفظته.
            # مطابقة نصّ الرسالة كانت ستنكسر عند أول إعادة صياغة أو ترجمة.
            return Response({
                'success': False,
                'code': 'insufficient_balance',
                'message': 'رصيدك لا يكفي لإكمال الحجز',
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّر إكمال الحجز')

    @swagger_auto_schema(
        operation_description="Buyer changes the delivery address on their own reservation. "
                               "Only allowed before the seller hands it off to the delivery "
                               "company (see send_to_delivery) — once an outbound delivery "
                               "order exists, the address is locked."
    )
    @action(detail=True, methods=['post'], url_path='update-delivery-location')
    def update_delivery_location(self, request, pk=None):
        reservation = self.get_object()

        if reservation.buyer != request.user:
            return Response({
                'success': False,
                'message': 'تغيير العنوان للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if reservation.delivery_orders.filter(direction='outbound').exists():
            return Response({
                'success': False,
                'message': 'أُرسل الطلب للتوصيل، فلا يمكن تغيير العنوان'
            }, status=status.HTTP_400_BAD_REQUEST)

        delivery_location_id = request.data.get('delivery_location_id')
        if not delivery_location_id:
            return Response({
                'success': False,
                'message': 'اختر عنوان التوصيل'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            location = Location.objects.get(id=delivery_location_id, user=request.user, is_active=True)
        except (Location.DoesNotExist, TypeError, ValueError):
            return Response({
                'success': False,
                'message': 'عنوان التوصيل غير موجود'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                reservation = Reservation.objects.select_for_update().select_related(
                    'item', 'buyer').get(pk=reservation.pk)
                if reservation.status not in ('pending', 'completed') or \
                        reservation.delivery_orders.filter(direction='outbound').exists():
                    return Response({
                        'success': False,
                        'message': 'لا يمكن تغيير العنوان الآن'
                    }, status=status.HTTP_400_BAD_REQUEST)
                reservation.delivery_location = location
                _reprice_delivery(reservation)
                reservation.save(update_fields=['delivery_location', 'delivery_fee'])
        except AddressNotChangeable as e:
            return Response({'success': False, 'message': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({
                'success': False,
                'code': 'insufficient_balance',
                'message': 'رصيدك لا يكفي لفرق أجرة التوصيل إلى العنوان الجديد'
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'success': True,
            'message': 'حُدّث عنوان التوصيل',
            'data': ReservationSerializer(reservation, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Approve and release payment to the seller"
    )
    @action(detail=True, methods=['post'])
    def approve_and_release_payment(self, request, pk=None):
        """Buyer approves and releases payment to the seller"""
        reservation = self.get_object()

        if reservation.buyer != request.user:
            return Response({
                'success': False,
                'message': 'تحرير الدفعة للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if reservation.status not in ('completed', 'confirmed'):
            return Response({
                'success': False,
                'message': 'لا يمكن تحرير دفعة هذا الطلب الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        if reservation.payment_released_to_seller:
            return Response({
                'success': False,
                'message': 'حُرّرت الدفعة مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        outbound_order = reservation.delivery_orders.filter(direction='outbound').first()
        # A deposit settles nothing by itself: the rest is collected at the door.
        # Handed over in person instead, the platform held 1% of the price and
        # took its 3% out of that; and two friends with a free order made
        # confirmed sales, with their reviews, out of nothing.
        if outbound_order is None and reservation.is_deposit_only:
            return Response({
                'success': False,
                'code': 'needs_delivery',
                'message': 'لم يُدفع ثمن هذا الطلب كاملاً — يُؤكَّد استلامه بعد توصيله عبر شركة التوصيل وتحصيل الباقي'
            }, status=status.HTTP_400_BAD_REQUEST)
        if outbound_order and outbound_order.status != 'confirmed':
            return Response({
                'success': False,
                'message': 'أكّد استلام المنتج أولاً من صفحة الطلب قبل تحرير الدفعة'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # Buyer confirming receipt is what settles the sale — move
                # past 'completed' into the final 'confirmed' state.
                _mark_reservation_received(reservation)

                # If this sale went through the delivery company, close out
                # its outbound leg too (mirrors DeliveryOrderViewSet.confirm_receipt).
                delivery_order = reservation.delivery_orders.filter(
                    direction='outbound', status__in=['assigned', 'picked_up', 'delivered']
                ).first()
                if delivery_order:
                    delivery_order.status = 'confirmed'
                    delivery_order.confirmed_at = timezone.now()
                    delivery_order.save(update_fields=['status', 'confirmed_at'])

            # Build success message
            message = 'حُرّرت الدفعة للبائع'

            return Response({
                'success': True,
                'message': message,
                'data': ReservationSerializer(reservation, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تحرير الدفعة')

    @swagger_auto_schema(
        operation_description="Seller hands a paid reservation off to the delivery company, instead of cancelling it."
    )
    @action(detail=True, methods=['post'], url_path='send-to-delivery')
    def send_to_delivery(self, request, pk=None):
        """Seller chooses to deliver (vs. cancel) — assigns the order to the delivery company"""
        reservation = self.get_object()

        is_seller = (
                reservation.item.owner == request.user or
                reservation.item.current_holder == request.user
        )
        if not is_seller:
            return Response({
                'success': False,
                'message': 'الإرسال للتوصيل للبائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if reservation.status != 'completed':
            return Response({
                'success': False,
                'message': 'يُرسل الطلب المدفوع فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not reservation.delivery_location:
            return Response({
                'success': False,
                'message': 'الطلب بلا عنوان توصيل'
            }, status=status.HTTP_400_BAD_REQUEST)

        if reservation.delivery_orders.filter(direction='outbound').exists():
            return Response({
                'success': False,
                'message': 'أُرسل الطلب للتوصيل مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        if reservation.handed_over_at:
            return Response({
                'success': False,
                'message': 'سلّمت هذا الطلب للمشتري يداً بيد'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not get_active_delivery_company():
            return Response({
                'success': False,
                'message': 'لا توجد شركة توصيل مُفعَّلة حالياً — أضِفها من لوحة الإدارة أولاً'
            }, status=status.HTTP_400_BAD_REQUEST)
        if resolve_pickup(reservation.item)[0] is None:
            return _no_pickup_address()

        try:
            with transaction.atomic():
                # Under the lock: four taps at once each saw no order yet and
                # sent four couriers, each to collect the cash.
                reservation = Reservation.objects.select_for_update().select_related(
                    'item', 'buyer', 'delivery_location').get(pk=reservation.pk)
                if (reservation.status != 'completed'
                        or reservation.delivery_orders.filter(direction='outbound').exists()):
                    return Response({
                        'success': False,
                        'message': 'أُرسل الطلب للتوصيل مسبقاً'
                    }, status=status.HTTP_400_BAD_REQUEST)
                delivery_order = create_outbound_delivery_order(reservation)

            return Response({
                'success': True,
                'message': 'تم إسناد التوصيل لشركة التوصيل',
                'data': DeliveryOrderSerializer(delivery_order).data
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر الإرسال للتوصيل')

    @swagger_auto_schema(
        operation_description="Seller records handing a fully paid order to the buyer face to face."
    )
    @action(detail=True, methods=['post'], url_path='goods-returned')
    def goods_returned(self, request, pk=None):
        """The seller has the piece back from the buyer of a handed-over order
        that was cancelled or refunded: it returns to stock now."""
        reservation = self.get_object()
        if request.user.id not in (reservation.item.owner_id, reservation.item.current_holder_id):
            return Response({'success': False, 'message': 'هذا الإجراء للبائع فقط'},
                            status=status.HTTP_403_FORBIDDEN)
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().select_related('item').get(pk=reservation.pk)
            why = reservation.goods_return_refusal()
            if why:
                return Response({'success': False, 'message': why}, status=status.HTTP_400_BAD_REQUEST)
            variant = reservation.stock_variant
            if variant is not None:
                ItemVariant.objects.select_for_update().get(pk=variant.pk).return_stock(reservation.quantity)
            reservation.goods_returned_at = timezone.now()
            reservation.save(update_fields=['goods_returned_at'])
            park_if_unlisted(Item.objects.select_for_update().get(pk=reservation.item_id))
        return Response({'success': True, 'message': 'أُعيدت القطعة إلى المخزون',
                         'data': ReservationSerializer(reservation, context={'request': request}).data})

    @action(detail=True, methods=['post'], url_path='hand-over')
    def hand_over(self, request, pk=None):
        """Without a courier leg, a paid order waited on the buyer's tap for
        ever, and a buyer who collected it inside the first two hours could
        still cancel it and get the money back. Pressing this ends both: the
        buyer can no longer cancel alone, and the warranty clock starts — a
        problem is a dispute, otherwise the money goes to the seller."""
        reservation = self.get_object()
        item = reservation.item
        if request.user.id not in (item.owner_id, item.current_holder_id):
            return Response({'success': False, 'message': 'التسليم للبائع فقط'},
                            status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().select_related(
                'item', 'buyer').get(pk=reservation.pk)
            refusal = reservation.hand_over_refusal()
            if refusal:
                return Response({'success': False, 'message': refusal},
                                status=status.HTTP_400_BAD_REQUEST)
            reservation.handed_over_at = timezone.now()
            # A prepaid delivery fee paid for a trip that will not happen.
            # Left in, release_reservation_payment takes it off the pot and,
            # with no courier leg, credits it to nobody.
            fee = reservation.delivery_fee
            if fee > 0:
                refund_to_buyer(reservation, fee, reason='أجرة توصيل لم يُحتج إليها — سُلّم يداً بيد')
                reservation.delivery_fee = Decimal('0.00')
            reservation.save(update_fields=['handed_over_at', 'delivery_fee'])

        days = reservation.warranty_days
        try:
            # A push, not the notifications socket: the app listens to FCM
            # only, and the dispute window starts now.
            back = (' وأُعيدت إليك أجرة التوصيل (%s د.ع).' % '{:,.0f}'.format(fee)) if fee > 0 else ''
            notify_push(
                reservation.buyer, 'سلّمك البائع الطلب',
                f'سجّل البائع أنه سلّمك "{bidi_isolate(reservation.item.title)}" يداً بيد. '
                f'إن لم تستلمه أو فيه مشكلة افتح نزاعاً خلال {days} يوم، وإلا يُحوَّل المبلغ للبائع.' + back,
                data={'type': 'reservation_handed_over', 'reservation_id': str(reservation.id)},
            )
        except Exception as e:
            logger.error(f"Failed to send hand-over notification: {e}")

        return Response({
            'success': True,
            'message': f'سُجّل التسليم — يُحوَّل المبلغ إليك بعد {days} يوم إن لم يفتح المشتري نزاعاً',
            'data': ReservationSerializer(reservation, context={'request': request}).data,
        })

    @swagger_auto_schema(
        operation_description="Seller bundles several paid reservations for the same buyer/address into as few delivery trips as fit the size cap — one delivery fee per trip instead of one per reservation."
    )
    @action(detail=False, methods=['post'], url_path='send-to-delivery-batch')
    def send_to_delivery_batch(self, request):
        """Same hand-off as send_to_delivery, but for several reservations at
        once — see delivery_utils.pack_into_delivery_groups/combine_deliveries
        for how they're grouped into trips and how the redundant per-item
        delivery fees get waived."""
        reservation_ids = request.data.get('reservation_ids', [])
        if not isinstance(reservation_ids, list) or len(reservation_ids) < 2:
            return Response({
                'success': False,
                'message': 'يجب اختيار حجزين على الأقل للدمج'
            }, status=status.HTTP_400_BAD_REQUEST)
        try:
            reservation_ids = [int(x) for x in reservation_ids]
        except (TypeError, ValueError):
            return Response({'success': False, 'message': 'طلب غير صالح'},
                            status=status.HTTP_400_BAD_REQUEST)

        reservations = list(
            Reservation.objects.filter(id__in=reservation_ids)
            .select_related('item', 'item__owner', 'item__current_holder', 'buyer', 'delivery_location')
        )
        if len(reservations) != len(set(reservation_ids)):
            return Response({
                'success': False,
                'message': 'بعض الحجوزات غير موجودة'
            }, status=status.HTTP_400_BAD_REQUEST)

        for r in reservations:
            is_seller = r.item.owner == request.user or r.item.current_holder == request.user
            if not is_seller:
                return Response({
                    'success': False,
                    'message': 'يمكن للبائع فقط دمج حجوزاته'
                }, status=status.HTTP_403_FORBIDDEN)
            if resolve_pickup(r.item)[0] is None:
                return _no_pickup_address()
            if r.status != 'completed':
                return Response({
                    'success': False,
                    'message': f'الحجز #{r.id} غير مكتمل الدفع بعد'
                }, status=status.HTTP_400_BAD_REQUEST)
            if not r.delivery_location_id:
                return Response({
                    'success': False,
                    'message': f'الحجز #{r.id} بدون عنوان توصيل'
                }, status=status.HTTP_400_BAD_REQUEST)
            if r.delivery_orders.filter(direction='outbound').exists():
                return Response({
                    'success': False,
                    'message': f'الحجز #{r.id} أُرسل للتوصيل مسبقاً'
                }, status=status.HTTP_400_BAD_REQUEST)

        first = reservations[0]
        if any(r.buyer_id != first.buyer_id or r.delivery_location_id != first.delivery_location_id
               for r in reservations):
            return Response({
                'success': False,
                'message': 'لا يمكن دمج حجوزات لزبائن مختلفين أو عناوين توصيل مختلفة'
            }, status=status.HTTP_400_BAD_REQUEST)

        company = get_active_delivery_company()
        if not company:
            return Response({
                'success': False,
                'message': 'لا توجد شركة توصيل مُفعَّلة حالياً — أضِفها من لوحة الإدارة أولاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        groups = pack_into_delivery_groups(reservations, company.max_combined_units)

        try:
            with transaction.atomic():
                # Same order everywhere, so two batches cannot deadlock.
                locked = list(Reservation.objects.select_for_update().filter(
                    id__in=[r.pk for r in reservations]).order_by('pk'))
                if any(r.status != 'completed' for r in locked) or DeliveryOrder.objects.filter(
                        reservation__in=locked, direction='outbound').exists():
                    return Response({
                        'success': False,
                        'message': 'تغيّرت حالة بعض الطلبات — حدّث الصفحة'
                    }, status=status.HTTP_400_BAD_REQUEST)
                results = [combine_deliveries(group, company) for group in groups]
        except Exception:
            return server_error('فشل دمج التوصيل')

        trips = len(results)
        total_refunded = sum((refunded for _batch, _orders, refunded in results), Decimal('0.00'))
        all_orders = [order for _batch, orders, _refunded in results for order in orders]

        return Response({
            'success': True,
            'message': (
                'تم دمج التوصيل برحلة واحدة برسم واحد'
                if trips == 1 else
                f'تجاوز الحجم المسموح لرحلة واحدة — تم تقسيمها إلى {trips} رحلات توصيل'
            ),
            'trips': trips,
            'refunded_amount': str(total_refunded),
            'data': DeliveryOrderSerializer(all_orders, many=True, context={'request': request}).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a reservation (buyer or seller can cancel)"""
        reservation = self.get_object()

        # Determine roles
        is_buyer = (reservation.buyer == request.user)
        is_seller = (
                reservation.item.owner == request.user or
                reservation.item.current_holder == request.user
        )

        if not is_buyer and not is_seller:
            return Response({
                'success': False,
                'message': 'الإلغاء للمشتري أو البائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # 'confirmed' is the final settled state (buyer confirmed receipt, or
        # 7 days passed, and payment already released to the seller) — it
        # cannot be cancelled by either side anymore.
        if reservation.status not in ['pending', 'completed']:
            return Response({
                'success': False,
                'message': 'لا يمكن إلغاء هذا الطلب الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Once the money has gone to the seller the sale is settled for both
        # sides — the buyer confirmed receipt (or the warranty ran out) and
        # the courier was paid. Nothing is reversible from here; a problem
        # after this point is a dispute, not a cancellation.
        if reservation.payment_released_to_seller:
            return Response({
                'success': False,
                'message': 'لا يمكن الإلغاء بعد تحرير المبلغ للبائع'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Sellers may cancel a reservation even after the buyer has paid the
        # deposit/full price ('completed') — e.g. they can no longer fulfill
        # it. The buyer is refunded automatically below from the escrow.
        if is_seller and not is_buyer:
            # ...but not once the delivery company has already picked the
            # item up — it's no longer physically in the seller's hands to
            # pull back, so cancelling out from under it makes no sense.
            outbound_order = reservation.delivery_orders.filter(direction='outbound').first()
            if outbound_order and outbound_order.status != 'assigned':
                return Response({
                    'success': False,
                    'message': 'لا يمكن الإلغاء — استلمت شركة التوصيل المنتج منك بالفعل'
                }, status=status.HTTP_400_BAD_REQUEST)
        else:
            # Buyer cancellation rules (unchanged)
            if not reservation.can_buyer_cancel():
                return Response({
                    'success': False,
                    # The app has no "approve" button for the seller; what
                    # they do have is their own cancel, which refunds in full.
                    'message': 'انتهت مهلة إلغائك للطلب — راسل البائع ليلغيه هو من صفحة الطلب، فيعود إليك المبلغ'
                }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # Re-read under a lock: the checks above ran outside the
                # transaction, and two concurrent cancels (or a cancel racing
                # the warranty release) would otherwise refund twice.
                reservation = Reservation.objects.select_for_update().select_related(
                    'item', 'buyer').get(pk=reservation.pk)
                if reservation.status not in ['pending', 'completed'] or reservation.payment_released_to_seller:
                    return Response({
                        'success': False,
                        'message': 'لا يمكن إلغاء هذا الطلب الآن'
                    }, status=status.HTTP_400_BAD_REQUEST)
                # The courier's leg is settled with the order: taken, it blocks
                # the cancel; not yet taken, it is called off — an order left
                # 'assigned' still sent the driver for a cancelled sale.
                outbound = DeliveryOrder.objects.select_for_update().filter(
                    reservation=reservation, direction='outbound',
                ).exclude(status__in=['cancelled', 'failed']).first()
                if outbound and outbound.status != 'assigned':
                    return Response({
                        'success': False,
                        'message': 'لا يمكن الإلغاء — استلمت شركة التوصيل المنتج بالفعل'
                    }, status=status.HTTP_400_BAD_REQUEST)
                if outbound:
                    hand_over_batch_fee(reservation, outbound)
                    outbound.status = 'cancelled'
                    outbound.save(update_fields=['status'])
                # An auction winner backing out alone forfeits the deposit, as
                # a refusal at the door does; with the seller's agreement, or
                # by the seller, it comes back.
                forfeit = bool(reservation.auction_id and is_buyer
                               and not reservation.seller_cancellation_approved)
                old_status = reservation.status
                stock_was_taken = old_status == 'completed' or reservation.stock_held

                # Stock is only taken at completion, so only restore it if
                # this reservation had actually reached that point.
                # Handed over in person, the piece is with the buyer: it goes
                # back on the shelf when the seller says they have it again.
                with_buyer = bool(reservation.handed_over_at)
                if stock_was_taken:
                    stock_variant = reservation.stock_variant
                    if stock_variant is not None and not with_buyer:
                        stock_variant = ItemVariant.objects.select_for_update().get(
                            pk=stock_variant.pk
                        )
                        stock_variant.return_stock(reservation.quantity)
                    if old_status == 'completed':
                        from ledger.services import void_app_sale
                        void_app_sale(reservation, 'أُلغي الحجز')
                        undo_sold_count(reservation)
                    item = Item.objects.select_for_update().get(id=reservation.item_id)
                    if with_buyer:
                        from .notifications import notify_goods_still_with_buyer
                        transaction.on_commit(lambda: notify_goods_still_with_buyer(reservation))

                    # This reservation may have been the one that sold out its
                    # live-session queue entry — now that stock is back,
                    # re-open that entry so viewers can reserve it again.
                    if reservation.live_session_item_id and not with_buyer:
                        LiveSessionItem.objects.filter(
                            id=reservation.live_session_item_id, status='sold_out',
                            session__status='live',
                        ).update(status='queued')
                else:
                    item = reservation.item
                park_if_unlisted(item)

                reservation.status = 'cancelled'
                reservation.save(update_fields=['status'])
                release_promotions(reservation)
                # An installment request still waiting for the admin's call
                # dies with the order — otherwise the admin could activate a
                # plan at Qi for a reservation that no longer exists.
                from .services.installments.service import cancel_open_requests
                cancel_open_requests(reservation, 'أُلغي الحجز')
                # A represented sale booked its commission as a pending row
                # at creation; nothing will ever release it now.
                cancel_pending_commission(reservation)
                if reservation.auction_id and old_status == 'pending':
                    settle_cancelled_auction_win(reservation, forfeit=forfeit)

                # Refund buyer's wallet for whatever they'd actually paid
                # The money is still in escrow (release is blocked above), so
                # the refund comes from the platform's held funds, nobody's wallet.
                refund_amount = reservation.amount_charged
                if forfeit and refund_amount > 0:
                    lost = min(reservation.deposit_amount, refund_amount)
                    if lost > 0:
                        forfeit_deposit(reservation, lost)
                        refund_amount -= lost
                if refund_amount > 0:
                    # Not necessarily the buyer's wallet: a financed order
                    # goes back to the financier instead, since that is who
                    # paid for it. See wallet_utils.refund_to_buyer.
                    refund_to_buyer(
                        reservation, refund_amount, item=item,
                        reason='إلغاء الحجز #%d' % reservation.pk,
                    )

            who = 'seller' if (is_seller and not is_buyer) else 'buyer'
            message = 'أُلغي الطلب'
            if stock_was_taken:
                message += '، ورُدّ المبلغ المستحق'

            # Send cancellation notifications
            try:
                if is_seller and not is_buyer:
                    # Seller cancelled → notify buyer
                    notify_reservation_cancelled_by_seller(reservation)
                    send_websocket_notification(
                        user_id=reservation.buyer.id,
                        notification_type='reservation_cancelled_by_seller',
                        title='تم إلغاء الحجز من البائع',
                        message=f'قام البائع بإلغاء حجزك لمنتج "{reservation.item.title}".',
                        data={'reservation_id': reservation.id, 'item_title': reservation.item.title}
                    )
                else:
                    # Buyer cancelled → notify seller
                    notify_reservation_cancelled_by_buyer(reservation)
                    seller = reservation.item.owner
                    send_websocket_notification(
                        user_id=seller.id,
                        notification_type='reservation_cancelled_by_buyer',
                        title='إلغاء حجز',
                        message=f'قام {reservation.buyer.username} بإلغاء حجزه لمنتجك "{reservation.item.title}".',
                        data={'reservation_id': reservation.id, 'item_title': reservation.item.title}
                    )
            except Exception as e:
                logger.error(f"Failed to send cancellation notification: {e}", exc_info=True)

            return Response({
                'success': True,
                'message': message,
                'data': ReservationSerializer(reservation, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر الإلغاء')

    @action(detail=True, methods=['post'])
    def approve_cancellation(self, request, pk=None):
        """Seller approves buyer's cancellation request"""
        reservation = self.get_object()

        # Check if seller is original owner OR current holder (for resale)
        is_seller = (
                reservation.item.owner == request.user or
                reservation.item.current_holder == request.user
        )

        if not is_seller:
            return Response({
                'success': False,
                'message': 'الموافقة على الإلغاء للبائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            reservation.seller_cancellation_approved = True
            reservation.save(update_fields=['seller_cancellation_approved'])

            return Response({
                'success': True,
                'message': 'وافقت على الإلغاء، ويستطيع المشتري إلغاء الطلب الآن',
                'data': ReservationSerializer(reservation, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّرت الموافقة على الإلغاء')

    @action(detail=True, methods=['post'])
    def request_payment_release(self, request, pk=None):
        """Seller requests buyer to release payment"""
        reservation = self.get_object()

        # Check if seller is original owner OR current holder (for resale)
        is_seller = (
                reservation.item.owner == request.user or
                (reservation.item.current_holder == request.user and reservation.is_transfer_resale)
        )

        if not is_seller:
            return Response({
                'success': False,
                'message': 'طلب الدفعة للبائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if reservation.status != 'completed':
            return Response({
                'success': False,
                'message': 'تُطلب دفعة الطلب المدفوع فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        if reservation.payment_released_to_seller:
            return Response({
                'success': False,
                'message': 'حُرّرت الدفعة مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        has_outbound_order = reservation.delivery_orders.filter(direction='outbound').exists()
        if not has_outbound_order and get_active_delivery_company():
            return Response({
                'success': False,
                'message': 'أسند الطلب لشركة التوصيل أولاً قبل مطالبة المشتري بتحرير الدفعة'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            from .tasks import send_payment_release_request_notification
            send_payment_release_request_notification.delay(
                reservation_id=reservation.id,
                seller_username=request.user.username,
                buyer_id=reservation.buyer.id
            )

            return Response({
                'success': True,
                'message': f'أُرسل طلب الدفعة إلى {reservation.buyer.username}'
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر إرسال طلب الدفعة')


class DeliveryOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Delivery legs handed off to the delivery company. The platform does not
    manage the company's drivers — this only exposes the coarse hand-off
    (assigned/picked_up/delivered) the company reports back, plus the
    buyer's own confirm/refuse-receipt actions.
    """
    queryset = DeliveryOrder.objects.all()
    serializer_class = DeliveryOrderSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = ItemStandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['status', 'direction', 'reservation', 'from_governorate']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DeliveryOrder.objects.none()

        user = self.request.user
        if user.is_staff or user.is_superuser:
            return DeliveryOrder.objects.all()

        company = getattr(user, 'delivery_company', None)
        if company:
            orders = DeliveryOrder.objects.filter(company=company)
            # The portal's finished tab: lately, not since the beginning.
            days = self.request.query_params.get('days', '')
            if days.isdigit() and 0 < int(days) <= 3650:
                orders = orders.filter(assigned_at__gte=timezone.now() - timedelta(days=int(days)))
            return orders

        return DeliveryOrder.objects.filter(
            Q(reservation__buyer=user) | Q(reservation__item__owner=user)
        )

    def _company_account_or_403(self, delivery_order, request):
        company = getattr(request.user, 'delivery_company', None)
        if not company or delivery_order.company_id != company.id:
            return Response({
                'success': False,
                'message': 'تحديث الطلب لشركة التوصيل المكلّفة فقط'
            }, status=status.HTTP_403_FORBIDDEN)
        return None

    @swagger_auto_schema(operation_description="Delivery company confirms it took physical custody of the item at the pickup address.")
    @action(detail=True, methods=['post'], url_path='pick-up')
    def pick_up(self, request, pk=None):
        """Company confirms pickup — for a return leg this is what releases the refund."""
        delivery_order = self.get_object()

        denied = self._company_account_or_403(delivery_order, request)
        if denied:
            return denied

        if delivery_order.status != 'assigned':
            return Response({
                'success': False,
                'message': 'لا يمكن استلام هذا الطلب الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # Same order as cancel(): reservation, then its leg.
                reservation = Reservation.objects.select_for_update().get(
                    pk=delivery_order.reservation_id)
                delivery_order = DeliveryOrder.objects.select_for_update().get(
                    pk=delivery_order.pk)
                if delivery_order.status != 'assigned' or (
                        delivery_order.direction == 'outbound'
                        and reservation.status in ('cancelled', 'refunded')):
                    return Response({
                        'success': False,
                        'message': 'أُلغي هذا الطلب ولا يُستلم'
                    }, status=status.HTTP_400_BAD_REQUEST)
                delivery_order.status = 'picked_up'
                delivery_order.picked_up_at = timezone.now()
                delivery_order.save(update_fields=['status', 'picked_up_at'])

                if delivery_order.direction == 'return':
                    execute_return_refund(delivery_order)

            return Response({
                'success': True,
                'message': 'تأكّد الاستلام',
                'data': DeliveryOrderSerializer(delivery_order).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تأكيد الاستلام')

    @swagger_auto_schema(operation_description="Delivery company confirms the item physically reached the buyer — prompts the buyer to confirm or refuse receipt.")
    @action(detail=True, methods=['post'], url_path='mark-arrived')
    def mark_arrived(self, request, pk=None):
        """Company signals the item is with the buyer now"""
        delivery_order = self.get_object()

        denied = self._company_account_or_403(delivery_order, request)
        if denied:
            return denied

        with transaction.atomic():
            delivery_order = DeliveryOrder.objects.select_for_update().get(pk=delivery_order.pk)
            if delivery_order.status != 'picked_up':
                return Response({
                    'success': False,
                    'message': 'لا يمكن تأكيد الوصول الآن'
                }, status=status.HTTP_400_BAD_REQUEST)

            delivery_order.status = 'delivered'
            delivery_order.delivered_at = timezone.now()
            delivery_order.save(update_fields=['status', 'delivered_at'])

        if delivery_order.direction == 'return':
            # Back with the seller. Administrative: the money was settled when
            # the goods left the buyer (execute_return_refund).
            return Response({
                'success': True,
                'message': 'أُغلق طلب الإرجاع',
                'data': DeliveryOrderSerializer(delivery_order).data
            }, status=status.HTTP_200_OK)

        try:
            notify_delivery_arrived(delivery_order)
        except Exception as e:
            logger.error(f"Failed to send delivery-arrived notification: {e}", exc_info=True)

        return Response({
            'success': True,
            'message': 'أُبلغ الزبون ليؤكّد الاستلام',
            'data': DeliveryOrderSerializer(delivery_order).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(operation_description="Delivery company could not deliver: it holds the goods and an admin decides the money.")
    @action(detail=True, methods=['post'], url_path='mark-failed')
    def mark_failed(self, request, pk=None):
        """The buyer was absent, the address wrong, the phone off. Without this
        the company could only say 'arrived', which starts the warranty clock
        and ends in the order counting as received. The goods are with the
        courier, so the return leg is already collected, and a dispute opens
        for an admin to settle the money (services.disputes)."""
        delivery_order = self.get_object()
        denied = self._company_account_or_403(delivery_order, request)
        if denied:
            return denied

        reason = str(request.data.get('reason') or '').strip()[:500]
        if not reason:
            return Response({'success': False, 'message': 'اكتب سبب تعذّر التوصيل'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                reservation = Reservation.objects.select_for_update().select_related(
                    'item', 'buyer').get(pk=delivery_order.reservation_id)
                delivery_order = DeliveryOrder.objects.select_for_update().get(pk=delivery_order.pk)
                if (delivery_order.direction != 'outbound'
                        or delivery_order.status not in ('picked_up', 'delivered')
                        or reservation.status != 'completed'
                        or reservation.payment_released_to_seller):
                    return Response({
                        'success': False,
                        'message': 'لا يمكن تسجيل تعذّر التوصيل لهذا الطلب الآن'
                    }, status=status.HTTP_400_BAD_REQUEST)
                delivery_order.status = 'failed'
                delivery_order.failure_reason = reason
                delivery_order.save(update_fields=['status', 'failure_reason'])
                reservation.status = 'disputed'
                reservation.save(update_fields=['status'])
                dispute = Dispute.objects.create(
                    item=reservation.item, reservation=reservation,
                    complainant=reservation.buyer, defendant=reservation.item.owner,
                    title='تعذّر التوصيل', description=reason, reason='delivery_failed')
                return_order = create_return_delivery_order(reservation, dispute=dispute)
                return_order.status = 'picked_up'
                return_order.picked_up_at = timezone.now()
                return_order.save(update_fields=['status', 'picked_up_at'])
        except Exception:
            return server_error('تعذّر تسجيل الحالة')

        try:
            notify_dispute_created(dispute)
        except Exception as e:
            logger.error(f"Failed to send delivery-failed notification: {e}", exc_info=True)
        return Response({
            'success': True,
            'message': 'سُجّل تعذّر التوصيل — أعد البضاعة للبائع، وستقرر الإدارة بشأن المبلغ',
            'data': DeliveryOrderSerializer(delivery_order).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(operation_description="Buyer confirms physical receipt of a delivered item — does not release payment.")
    @action(detail=True, methods=['post'], url_path='confirm-receipt')
    def confirm_receipt(self, request, pk=None):
        """
        Buyer presses 'received' — confirms physical receipt AND, for a COD
        order, that they paid the courier the remainder in cash (accepting
        the item and paying happen in the same moment — a real courier
        wouldn't hand it over otherwise). This is what makes the remainder
        count toward Reservation.amount_charged (releasable to the seller)
        and the company's cash debt to the platform.

        This deliberately does NOT release payment itself: the held amount
        is only released after the item's warranty period passes with no
        open dispute (tasks.release_payments_after_week) or when the buyer
        separately chooses to release it early via
        ReservationViewSet.approve_and_release_payment (only allowed once
        this delivery order is 'confirmed' — see get_can_approve).
        """
        delivery_order = self.get_object()

        if delivery_order.reservation.buyer != request.user:
            return Response({
                'success': False,
                'message': 'تأكيد الاستلام للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if delivery_order.direction != 'outbound' or delivery_order.status != 'delivered':
            return Response({
                'success': False,
                'message': 'لا يمكن تأكيد الاستلام الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # Re-read: a refusal may have landed since.
            Reservation.objects.select_for_update().get(pk=delivery_order.reservation_id)
            delivery_order = DeliveryOrder.objects.select_for_update().get(pk=delivery_order.pk)
            if delivery_order.status != 'delivered':
                return Response({
                    'success': False,
                    'message': 'لا يمكن تأكيد الاستلام الآن'
                }, status=status.HTTP_400_BAD_REQUEST)
            record_receipt(delivery_order)

        if delivery_order.cash_collected:
            try:
                notify_cash_collected(delivery_order)
            except Exception as e:
                logger.error(f"Failed to send cash-collected notification: {e}", exc_info=True)

        from .notifications import format_days_ar
        warranty_period = format_days_ar(delivery_order.reservation.warranty_days)
        return Response({
            'success': True,
            'message': f'تم تأكيد الاستلام. سيُحرَّر المبلغ للبائع تلقائياً خلال {warranty_period}، أو يمكنك تحريره الآن إن لم يكن لديك أي مشكلة',
            'data': DeliveryOrderSerializer(delivery_order).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(operation_description="Buyer refuses a delivered item at the door. With a stated reason this opens a support-reviewed dispute (full refund is the admin's call); with no reason it's a plain refusal and the deposit (+ delivery fee if paid in full) is forfeited once the company confirms it collected the item back.")
    @action(detail=True, methods=['post'], url_path='refuse-receipt')
    def refuse_receipt(self, request, pk=None):
        """
        Buyer presses 'refuse receipt'. Whether a `reason` was given decides
        which of two very different outcomes this leads to once the return
        leg's pick-up fires execute_return_refund:
          - reason given -> a real Dispute is opened (support/admin decides
            the outcome via DisputeViewSet.resolve — same as any dispute).
          - no reason -> plain refusal, no complaint on record. The buyer
            forfeits their deposit (and the delivery fee too if they paid in
            full) — the seller and delivery company still did their part.
        """
        delivery_order = self.get_object()
        reservation = delivery_order.reservation

        if reservation.buyer != request.user:
            return Response({
                'success': False,
                'message': 'رفض الاستلام للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if delivery_order.direction != 'outbound' or delivery_order.status != 'delivered':
            return Response({
                'success': False,
                'message': 'لا يمكن رفض الاستلام الآن'
            }, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', '').strip()

        try:
            with transaction.atomic():
                # Settled already (released, or received), there is nothing at
                # the door to refuse: a complaint from here is a dispute.
                # Refusing after release paid the seller the deposit again.
                reservation = Reservation.objects.select_for_update().select_related(
                    'item', 'buyer').get(pk=reservation.pk)
                delivery_order = DeliveryOrder.objects.select_for_update().get(
                    pk=delivery_order.pk)
                if (delivery_order.status != 'delivered' or reservation.status != 'completed'
                        or reservation.payment_released_to_seller):
                    return Response({
                        'success': False,
                        'message': 'انتهت تسوية هذا الطلب — افتح نزاعاً إن كانت لديك مشكلة'
                    }, status=status.HTTP_400_BAD_REQUEST)
                delivery_order.status = 'refused'
                delivery_order.refused_at = timezone.now()
                delivery_order.failure_reason = reason
                delivery_order.save(update_fields=['status', 'refused_at', 'failure_reason'])

                # Blocks cancel()/approve_and_release_payment() until the
                # return leg's pickup settles this reservation to 'refunded'.
                reservation.status = 'disputed'
                reservation.save(update_fields=['status'])

                dispute = None
                if reason:
                    dispute = Dispute.objects.create(
                        item=reservation.item,
                        reservation=reservation,
                        complainant=reservation.buyer,
                        defendant=reservation.item.owner,
                        title='رفض استلام عند التوصيل',
                        description=reason,
                        reason='delivery_refused',
                    )

                return_order = create_return_delivery_order(reservation, dispute=dispute)

            try:
                if dispute:
                    notify_dispute_created(dispute)
                else:
                    notify_delivery_refused(delivery_order)
            except Exception as e:
                logger.error(f"Failed to send delivery-refused notification: {e}", exc_info=True)

            message = (
                'تم رفض الاستلام وفتح نزاع — سيراجعه فريق الدعم ويقرر بشأن استرداد المبلغ'
                if dispute else
                'تم رفض الاستلام — سيُخصَم العربون (وأجور التوصيل إن دفعتها كاملة) بعد استلام الشركة للمنتج منك، ويُعاد الباقي'
            )
            return Response({
                'success': True,
                'message': message,
                'data': DeliveryOrderSerializer(return_order).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر رفض الاستلام')


class DisputeViewSet(viewsets.ModelViewSet):
    """Manage disputes"""
    queryset = Dispute.objects.all()
    serializer_class = DisputeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Dispute.objects.none()

        disputes = Dispute.objects.order_by('-created_at', '-id')
        if self.action == 'list':
            disputes = user_related(reservation_related(item_related(disputes, 'item'), 'reservation'),
                                    'complainant', 'defendant').prefetch_related('replies__images', 'replies__author')

        # Users see disputes they're involved in
        if self.request.user.is_staff:
            return disputes

        return disputes.filter(
            Q(complainant=self.request.user) |
            Q(defendant=self.request.user)
        )

    def destroy(self, request, *args, **kwargs):
        # Disputes are a legal/audit record for both parties — neither the
        # complainant, the defendant, nor staff may erase one. resolve()
        # (admin-only) is the only way a dispute is ever closed out.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف النزاع. يتم إغلاقه فقط عبر قرار الإدارة (resolve).'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # Create and update are closed too. A dispute is opened by create_dispute
    # (which checks the complainant is the buyer, not the seller) and decided
    # by resolve (admins only). A plain PATCH let the defendant close the
    # complaint against them with a fake resolution and "admin notes", which
    # took it out of the open-disputes queue the admins work from.
    def _closed(self):
        return Response({
            'success': False,
            'message': 'افتح النزاع عبر create_dispute، ويُغلق فقط بقرار الإدارة (resolve).'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def create(self, request, *args, **kwargs):
        return self._closed()

    def update(self, request, *args, **kwargs):
        return self._closed()

    def partial_update(self, request, *args, **kwargs):
        return self._closed()

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser, JSONParser])
    def reply(self, request, pk=None):
        """Either side's words and pictures, while the dispute is open."""
        dispute = self.get_object()
        if request.user.id not in (dispute.complainant_id, dispute.defendant_id):
            return Response({'success': False, 'message': 'لست طرفاً في هذا النزاع'},
                            status=status.HTTP_403_FORBIDDEN)
        text = str(request.data.get('text') or '').strip()
        images = request.FILES.getlist('images')
        try:
            with transaction.atomic():
                dispute = Dispute.objects.select_for_update().get(pk=dispute.pk)
                add_reply(dispute, request.user, text, images)
        except DjangoValidationError as exc:
            return Response({'success': False, 'message': exc.messages[0]},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'success': True, 'message': 'أُرسل ردّك',
                         'data': DisputeSerializer(dispute, context={'request': request}).data},
                        status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        operation_description="Create a dispute for an item or reservation",
        request_body=CreateDisputeSerializer,
        responses={
            201: DisputeSerializer,
            400: "Validation failed",
            404: "Item not found"
        }
    )
    @action(detail=False, methods=['post'])
    def create_dispute(self, request):
        """Create a new dispute"""
        serializer = CreateDisputeSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data['item_id']
        reservation_id = serializer.validated_data.get('reservation_id')
        title = serializer.validated_data['title']
        description = serializer.validated_data['description']
        reason = serializer.validated_data.get('reason', '')

        try:
            item = Item.objects.get(id=item_id)
            if not reservation_id:
                return Response({
                    'success': False,
                    'message': 'افتح النزاع من صفحة الطلب نفسه'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Determine defendant based on whether user is buyer or seller
            if item.owner == request.user:
                # Seller filing dispute - need to find buyer from reservation
                if reservation_id:
                    reservation = Reservation.objects.get(id=reservation_id, item=item)
                    defendant = reservation.buyer
                else:
                    return Response({
                        'success': False,
                        'message': 'حدّد الطلب لفتح النزاع'
                    }, status=status.HTTP_400_BAD_REQUEST)
            else:
                # Buyer filing dispute against seller
                defendant = item.owner
                reservation = None
                if reservation_id:
                    reservation = Reservation.objects.get(id=reservation_id, item=item, buyer=request.user)

            # Disputes are the only channel for a "the item hasn't arrived /
            # isn't right" complaint on a paid reservation — a buyer must not
            # be able to get the same result through cancel(). Allowed while
            # 'completed' (paid, awaiting receipt confirmation) or 'confirmed'
            # (buyer already has the item — e.g. a defect discovered after
            # receipt; a won dispute here returns the item via the delivery
            # company before any refund fires — see DeliveryOrderViewSet.pick_up).
            # Blocked before payment and once otherwise closed out.
            if reservation and reservation.status not in ('completed', 'confirmed'):
                return Response({
                    'success': False,
                    'message': 'لا يمكن فتح نزاع على هذا الطلب'
                }, status=status.HTTP_400_BAD_REQUEST)

            # A settled sale stays settled: the warranty, plus a few days for
            # a complaint about its last day.
            if reservation and reservation.status == 'confirmed':
                deadline = reservation.dispute_deadline
                if deadline and timezone.now() > deadline:
                    return Response({
                        'success': False,
                        'message': 'انتهت مدة الضمان، ولا يمكن فتح نزاع على هذا الطلب'
                    }, status=status.HTTP_400_BAD_REQUEST)

            with transaction.atomic():
                if reservation:
                    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
                    if reservation.status not in ('completed', 'confirmed'):
                        return Response({
                            'success': False,
                            'message': 'يوجد نزاع على هذا الطلب مسبقاً'
                        }, status=status.HTTP_400_BAD_REQUEST)
                    # A decided complaint re-filed for ever keeps the seller's
                    # money and the admins' queue hostage.
                    if Dispute.objects.filter(reservation=reservation,
                                              complainant=request.user).count() >= 2:
                        return Response({
                            'success': False,
                            'message': 'حُسم أكثر من نزاع على هذا الطلب — تواصل مع الدعم'
                        }, status=status.HTTP_400_BAD_REQUEST)
                dispute = Dispute.objects.create(
                    item=item,
                    reservation=reservation,
                    complainant=request.user,
                    defendant=defendant,
                    title=title,
                    description=description,
                    reason=reason
                )
                if reservation:
                    reservation.status = 'disputed'
                    reservation.save(update_fields=['status'])

            return Response({
                'success': True,
                'message': 'فُتح النزاع',
                'data': DisputeSerializer(dispute).data
            }, status=status.HTTP_201_CREATED)

        except Item.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Reservation.DoesNotExist:
            return Response({
                'success': False,
                'message': 'الطلب غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            return server_error('تعذّر فتح النزاع')

    @swagger_auto_schema(
        operation_description="Resolve a dispute (admin only)",
        request_body=ResolveDisputeSerializer,
        responses={
            200: DisputeSerializer,
            400: "Invalid resolution",
            403: "Permission denied",
            404: "Dispute not found"
        }
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def resolve(self, request, pk=None):
        """Admin resolves a dispute"""
        dispute = self.get_object()
        serializer = ResolveDisputeSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        resolution = serializer.validated_data['resolution']
        resolution_action = serializer.validated_data['resolution_action']
        admin_notes = serializer.validated_data.get('admin_notes', '')
        partial_refund_amount = serializer.validated_data.get('partial_refund_amount')

        try:
            resolve_dispute(
                dispute,
                resolution_action=resolution_action,
                resolution=resolution,
                admin_notes=admin_notes,
                partial_refund_amount=partial_refund_amount,
            )

            return Response({
                'success': True,
                'message': 'حُسم النزاع',
                'data': DisputeSerializer(dispute).data
            }, status=status.HTTP_200_OK)

        except DisputeResolutionError as e:
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e:
            return Response({
                'success': False,
                'message': 'تعذّر تنفيذ التحويل المالي لهذا القرار',
                'errors': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّر حسم النزاع')


class ReportViewSet(viewsets.ModelViewSet):
    """Report items, stores, or users for violating platform policy"""
    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_class = ReportFilter
    ordering_fields = ['created_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Report.objects.none()

        qs = Report.objects.select_related(
            'reporter', 'reported_item', 'reported_store', 'reported_user', 'reviewed_by'
        )
        if self.request.user.is_staff:
            return qs

        # A regular user only ever sees reports they filed themselves —
        # never reports filed against them, which would tip off a bad actor
        # about who reported them and what was said.
        return qs.filter(reporter=self.request.user)

    def destroy(self, request, *args, **kwargs):
        # A report is the raw input to a moderation decision — the reporter
        # withdrawing or hiding it after the fact shouldn't erase that
        # record. resolve() (admin-only) is the only way one is closed out.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف البلاغ. يتم إغلاقه فقط عبر قرار الإدارة (resolve).'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @swagger_auto_schema(
        operation_description="Report an item, a store, or a user for a policy violation",
        request_body=CreateReportSerializer,
        responses={
            201: ReportSerializer,
            400: "Validation failed",
        }
    )
    @action(detail=False, methods=['post'])
    @one_at_a_time('report')
    def create_report(self, request):
        """File a new report"""
        serializer = CreateReportSerializer(data=request.data, context={'request': request})

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        target_type = serializer.validated_data['target_type']
        target = serializer.validated_data['target']
        target_field = {
            'item': 'reported_item', 'store': 'reported_store',
            'user': 'reported_user', 'reel': 'reported_reel',
        }[target_type]

        # Don't let the same reporter pile up duplicate reports on the same
        # target while an earlier one is still open.
        already_reported = Report.objects.filter(
            reporter=request.user, status__in=['pending', 'reviewing'], **{target_field: target}
        ).exists()
        if already_reported:
            return Response({
                'success': False,
                'message': 'لديك بلاغ قيد المراجعة بالفعل عن هذا العنصر'
            }, status=status.HTTP_400_BAD_REQUEST)

        report = Report.objects.create(
            reporter=request.user,
            reason=serializer.validated_data['reason'],
            description=serializer.validated_data.get('description', ''),
            **{target_field: target}
        )

        notify_report_submitted(report)

        return Response({
            'success': True,
            'message': 'تم إرسال البلاغ بنجاح، سيقوم فريقنا بمراجعته',
            'data': ReportSerializer(report, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @swagger_auto_schema(
        operation_description="Resolve a report (admin only)",
        request_body=ResolveReportSerializer,
        responses={
            200: ReportSerializer,
            400: "Invalid resolution",
            403: "Permission denied",
            404: "Report not found"
        }
    )
    @action(detail=True, methods=['post'], permission_classes=[IsAdminUser])
    def resolve(self, request, pk=None):
        """Admin closes out a report, optionally applying a moderation action"""
        report = self.get_object()
        serializer = ResolveReportSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            resolve_report(
                report,
                new_status=serializer.validated_data['status'],
                action_taken=serializer.validated_data.get('action_taken', 'none'),
                admin_notes=serializer.validated_data.get('admin_notes', ''),
                actor=request.user,
            )
        except ReportResolutionError as e:
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)

        notify_report_reviewed(report)

        return Response({
            'success': True,
            'message': 'تم تحديث حالة البلاغ',
            'data': ReportSerializer(report, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class RepresentativeViewSet(viewsets.ModelViewSet):
    """Manage representative requests and assignments"""
    queryset = Representative.objects.all()
    serializer_class = RepresentativeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_fields = ['status', 'item__id', 'owner__id', 'representative__id']
    search_fields = [
        'representative__username',
        'representative__email',
        'owner__username',
        'item__title'
    ]
    ordering_fields = ['created_at', 'commission_percentage', 'status']

    def get_queryset(self):
        """Filter queryset based on user"""
        if getattr(self, 'swagger_fake_view', False):
            return Representative.objects.none()

        user = self.request.user

        # For create_reservation_for_buyer action, allow anyone to access
        # active representative assignments (buyers need to see them)
        if self.action == 'create_reservation_for_buyer':
            return Representative.objects.filter(
                status__in=['accepted', 'active'],
                can_complete_sale=True
            ).select_related('item', 'owner', 'representative')

        # For other actions, show requests where user is owner OR representative
        requests = Representative.objects.filter(
            Q(owner=user) | Q(representative=user)
        ).select_related('item', 'owner', 'representative')
        if self.action in ('list', 'received_requests', 'sent_requests', 'active'):
            requests = user_related(item_related(requests, 'item'), 'owner', 'representative')
        return requests

    def get_serializer_class(self):
        if self.action == 'create_request':
            return RepresentativeCreateSerializer
        elif self.action == 'accept':
            return RepresentativeAcceptSerializer
        elif self.action == 'reject':
            return RepresentativeRejectSerializer
        elif self.action == 'update_permissions':
            return RepresentativeUpdatePermissionsSerializer
        return RepresentativeSerializer

    @swagger_auto_schema(
        operation_description="Get requests where user is the owner (received requests)",
        responses={200: RepresentativeSerializer(many=True)}
    )
    @action(detail=False, methods=['get'])
    def received_requests(self, request):
        """Get all representative requests received by the current user"""
        requests = self.get_queryset().filter(
            owner=request.user,
            status='pending'
        )
        serializer = self.get_serializer(requests, many=True)
        return Response({
            'success': True,
            'count': requests.count(),
            'data': serializer.data
        })

    @swagger_auto_schema(
        operation_description="Get requests where user is the representative (sent requests)",
        responses={200: RepresentativeSerializer(many=True)}
    )
    @action(detail=False, methods=['get'])
    def sent_requests(self, request):
        """Get all representative requests sent by the current user"""
        requests = self.get_queryset().filter(representative=request.user)
        serializer = self.get_serializer(requests, many=True)
        return Response({
            'success': True,
            'count': requests.count(),
            'data': serializer.data
        })

    @swagger_auto_schema(
        operation_description="Get active representative assignments",
        responses={200: RepresentativeSerializer(many=True)}
    )
    @action(detail=False, methods=['get'])
    def active(self, request):
        """Get all active representative assignments"""
        active_reps = self.get_queryset().filter(
            Q(representative=request.user) | Q(owner=request.user),
            status__in=['accepted', 'active']
        )
        serializer = self.get_serializer(active_reps, many=True)
        return Response({
            'success': True,
            'count': active_reps.count(),
            'data': serializer.data
        })

    @swagger_auto_schema(
        operation_description="Create a representative request for an item",
        request_body=RepresentativeCreateSerializer,
        responses={
            201: RepresentativeSerializer,
            400: "Validation failed",
            403: "Permission denied",
            404: "Item not found"
        }
    )
    @action(detail=False, methods=['post'])
    @one_at_a_time('representative')
    def create_request(self, request):
        """User requests to become a representative for an item"""
        serializer = RepresentativeCreateSerializer(
            data=request.data,
            context={'request': request}
        )

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data['item_id']
        commission_percentage = serializer.validated_data['commission_percentage']
        can_list = serializer.validated_data.get('can_list', True)
        can_negotiate = serializer.validated_data.get('can_negotiate', True)
        can_complete_sale = serializer.validated_data.get('can_complete_sale', True)
        request_message = serializer.validated_data.get('request_message', '')

        try:
            item = Item.objects.get(id=item_id)

            # Create representative request
            representative = Representative.objects.create(
                item=item,
                owner=item.owner,
                representative=request.user,
                commission_percentage=commission_percentage,
                can_list=can_list,
                can_negotiate=can_negotiate,
                can_complete_sale=can_complete_sale,
                request_message=request_message,
                status='pending'
            )

            # Send notification to owner
            from .tasks import send_representative_request_notification
            send_representative_request_notification.delay(representative.id)

            return Response({
                'success': True,
                'message': 'أُرسل طلب المندوبية',
                'data': RepresentativeSerializer(representative, context={'request': request}).data
            }, status=status.HTTP_201_CREATED)

        except Item.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            return server_error('تعذّر إرسال طلب المندوبية')

    @swagger_auto_schema(
        operation_description="Accept a representative request (owner only)",
        responses={
            200: RepresentativeSerializer,
            400: "Cannot accept request",
            403: "Permission denied",
            404: "Request not found"
        }
    )
    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        """Item owner accepts a representative request"""
        representative = self.get_object()

        # Only owner can accept
        if representative.owner != request.user:
            return Response({
                'success': False,
                'message': 'القبول لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Must be pending
        if representative.status != 'pending':
            return Response({
                'success': False,
                'message': 'لا يمكن قبول هذا الطلب'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                representative.status = 'accepted'
                representative.accepted_at = timezone.now()
                representative.save(update_fields=['status', 'accepted_at'])

                # Notify representative
                from .tasks import send_representative_accepted_notification
                send_representative_accepted_notification.delay(representative.id)

            return Response({
                'success': True,
                'message': 'قُبل طلب المندوبية',
                'data': RepresentativeSerializer(representative, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر قبول الطلب')

    @swagger_auto_schema(
        operation_description="Reject a representative request (owner only)",
        request_body=RepresentativeRejectSerializer,
        responses={
            200: RepresentativeSerializer,
            400: "Cannot reject request",
            403: "Permission denied",
            404: "Request not found"
        }
    )
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Item owner rejects a representative request"""
        representative = self.get_object()

        # Only owner can reject
        if representative.owner != request.user:
            return Response({
                'success': False,
                'message': 'الرفض لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Must be pending
        if representative.status != 'pending':
            return Response({
                'success': False,
                'message': 'لا يمكن رفض هذا الطلب'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = RepresentativeRejectSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        rejection_reason = serializer.validated_data.get('rejection_reason', '')

        try:
            with transaction.atomic():
                representative.status = 'rejected'
                representative.rejected_at = timezone.now()
                representative.rejection_reason = rejection_reason
                representative.save(update_fields=['status', 'rejected_at', 'rejection_reason'])

                # Notify representative
                from .tasks import send_representative_rejected_notification
                send_representative_rejected_notification.delay(representative.id)

            return Response({
                'success': True,
                'message': 'رُفض طلب المندوبية',
                'data': RepresentativeSerializer(representative, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر رفض الطلب')

    @swagger_auto_schema(
        operation_description="Cancel a representative request or assignment",
        responses={
            200: RepresentativeSerializer,
            400: "Cannot cancel",
            403: "Permission denied",
            404: "Request not found"
        }
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a representative request or assignment"""
        representative = self.get_object()

        # Representative can cancel pending requests
        # Owner can cancel accepted/active assignments
        can_cancel = (
            (representative.representative == request.user and representative.status in ['pending','accepted']) or
            (representative.owner == request.user and representative.status in ['accepted', 'active'])
        )

        if not can_cancel:
            return Response({
                'success': False,
                'message': 'لا يمكنك إلغاء هذا'
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            with transaction.atomic():
                representative.status = 'cancelled'
                representative.cancelled_at = timezone.now()
                representative.save(update_fields=['status', 'cancelled_at'])

                # Notify the other party
                from .tasks import send_representative_cancelled_notification
                send_representative_cancelled_notification.delay(representative.id, request.user.id)

            return Response({
                'success': True,
                'message': 'أُلغيت المندوبية',
                'data': RepresentativeSerializer(representative, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر الإلغاء')

    @swagger_auto_schema(
        operation_description="Create a reservation through a representative (buyer gets representative's deal)",
        request_body=CreateReservationSerializer,
        responses={
            201: ReservationSerializer,
            400: "Validation failed or item unavailable",
            403: "Permission denied or representative not authorized",
            404: "Item, listing, or representative assignment not found",
            500: "Internal server error"
        }
    )
    @action(detail=True, methods=['post'])
    def create_reservation_for_buyer(self, request, pk=None):
        """
        Buyer creates a reservation through this representative.
        The representative gets their commission from this sale.
        pk = representative assignment ID
        """
        representative_assignment = self.get_object()

        # Verify representative assignment is active
        if representative_assignment.status not in ['accepted', 'active']:
            return Response({
                'success': False,
                'message': 'المندوبية غير نشطة'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Verify representative can complete sales
        if not representative_assignment.representative.is_active:
            return Response({
                'success': False,
                'message': 'المندوبية غير نشطة'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not representative_assignment.can_complete_sale:
            return Response({
                'success': False,
                'message': 'هذا المندوب غير مخوّل بالبيع'
            }, status=status.HTTP_403_FORBIDDEN)

        serializer = CreateReservationSerializer(
            data=request.data,
            context={'request': request}
        )

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data['item_id']
        quantity = serializer.validated_data.get('quantity', 1)

        # Verify the item_id matches the representative's item
        if representative_assignment.item.id != item_id:
            return Response({
                'success': False,
                'message': 'المنتج لا يخص هذه المندوبية'
            }, status=status.HTTP_400_BAD_REQUEST)

        item = representative_assignment.item

        # Buyer cannot be the owner
        if item.owner == request.user:
            return Response({
                'success': False,
                'message': 'لا يمكنك حجز منتجك'
            }, status=status.HTTP_403_FORBIDDEN)

        # Buyer cannot be the representative
        if representative_assignment.representative == request.user:
            return Response({
                'success': False,
                'message': 'لا يمكن للمندوب الشراء من رابطه'
            }, status=status.HTTP_403_FORBIDDEN)

        if quantity < 1:
            return Response({
                'success': False,
                'message': 'الكمية واحد على الأقل'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # Lock the item
                item = Item.objects.select_for_update().get(id=item_id)

                try:
                    fixed_listing = item.fixed_listing
                except FixedPriceListing.DoesNotExist:
                    return Response({
                        'success': False,
                        'message': 'المنتج غير معروض بسعر ثابت'
                    }, status=status.HTTP_404_NOT_FOUND)

                # Validation checks
                if item.status not in ['active', 'reserved']:
                    return Response({
                        'success': False,
                        'message': 'المنتج غير متاح للحجز'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # The combination the buyer picked, as in create_reservation:
                # this path sold the default one at the listing price.
                variant, variant_error = _resolve_variant(
                    item, serializer.validated_data.get('variant_id'))
                if variant_error:
                    return Response({'success': False, 'message': variant_error},
                                    status=status.HTTP_400_BAD_REQUEST)
                if quantity > variant.quantity:
                    return Response({
                        'success': False,
                        'message': f'المتوفّر {variant.quantity} فقط'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Check for existing active reservation by this user
                existing = Reservation.objects.filter(
                    item=item,
                    buyer=request.user,
                    status='pending'
                ).exists()

                if existing:
                    return Response({
                        'success': False,
                        'message': 'لديك حجز قائم على هذا المنتج'
                    }, status=status.HTTP_400_BAD_REQUEST)

                # Calculate amounts
                if variant.price is not None:
                    total_price = variant.price * quantity
                    deposit_amount = total_price * fixed_listing.deposit_percentage / Decimal('100')
                else:
                    total_price = fixed_listing.price * quantity
                    deposit_amount = fixed_listing.deposit_amount * quantity

                if total_price > MAX_AMOUNT:
                    return Response({'success': False, 'message': 'المبلغ أكبر من المسموح — قلّل الكمية'},
                                    status=status.HTTP_400_BAD_REQUEST)

                # Commission is calculated on total sale price, not on deposit
                commission_amount = (total_price * representative_assignment.commission_percentage) / 100

                expiry_time = timezone.now() + timedelta(
                    hours=getattr(settings, 'RESERVATION_EXPIRY_HOURS', 24)
                )

                # Create reservation WITH representative info
                from .services.commission import commission_for
                platform_percent, platform_amount = commission_for(item, total_price)
                reservation = Reservation.objects.create(
                    item=item,
                    variant=variant,
                    buyer=request.user,
                    quantity=quantity,
                    deposit_amount=deposit_amount,
                    total_price=total_price,
                    expires_at=expiry_time,
                    representative=representative_assignment.representative,
                    representative_assignment=representative_assignment,
                    warranty_days=item.warranty_days,
                    commission_amount=commission_amount,
                    platform_commission_percent=platform_percent,
                    platform_commission_amount=platform_amount,
                )

                # === NEW: Create pending commission transaction for representative ===
                Transaction.objects.create(
                    user=representative_assignment.representative,
                    transaction_type='commission',
                    amount=commission_amount,
                    status='pending',  # Will be completed when payment is released
                    item=item,
                    reservation=reservation
                )

                # The stock is taken when the buyer completes, as for any order.

                # Notify representative about the sale
                from .tasks import send_representative_sale_notification
                send_representative_sale_notification.delay(
                    reservation.id,
                    representative_assignment.id
                )

                # Notify owner
                from .tasks import send_reservation_created_notification
                send_reservation_created_notification.delay(
                    reservation.id,
                    item.owner.id
                )

            return Response({
                'success': True,
                'message': f'تم الحجز عبر المندوب {representative_assignment.representative.username}',
                'data': ReservationSerializer(reservation, context={'request': request}).data,
                'representative_info': {
                    'username': representative_assignment.representative.username,
                    'commission_percentage': str(representative_assignment.commission_percentage),
                    'commission_amount': str(commission_amount),
                    'commission_status': 'pending'
                }
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر إنشاء الحجز')


    @swagger_auto_schema(
        operation_description="Update representative permissions (owner only)",
        request_body=RepresentativeUpdatePermissionsSerializer,
        responses={
            200: RepresentativeSerializer,
            400: "Validation failed",
            403: "Permission denied",
            404: "Request not found"
        }
    )
    @action(detail=True, methods=['patch'])
    def update_permissions(self, request, pk=None):
        """Owner updates representative permissions"""
        representative = self.get_object()

        # Only owner can update permissions
        if representative.owner != request.user:
            return Response({
                'success': False,
                'message': 'تعديل الصلاحيات لصاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Must be active
        if representative.status not in ['accepted', 'active']:
            return Response({
                'success': False,
                'message': 'تُعدّل صلاحيات المندوب النشط فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = RepresentativeUpdatePermissionsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            update_fields = []

            if 'can_list' in serializer.validated_data:
                representative.can_list = serializer.validated_data['can_list']
                update_fields.append('can_list')

            if 'can_negotiate' in serializer.validated_data:
                representative.can_negotiate = serializer.validated_data['can_negotiate']
                update_fields.append('can_negotiate')

            if 'can_complete_sale' in serializer.validated_data:
                representative.can_complete_sale = serializer.validated_data['can_complete_sale']
                update_fields.append('can_complete_sale')

            # النسبة اتفاقٌ لا صلاحية: اقترحها المندوب ووافق عليها البائع،
            # وعليها يبيع المندوب. لو غيّرها البائع بعد الموافقة لخفّض حصّة
            # مندوب يعمل على وعدٍ أُعطي له. النسخ القديمة من التطبيق ترسل
            # النسبة الحالية مع كل حفظ، فالقيمة نفسها تُقبل وتُتجاهَل، وأي
            # قيمة أخرى تُرفض: نسبة جديدة تعني طلب مندوبية جديداً.
            new_rate = serializer.validated_data.get('commission_percentage')
            if new_rate is not None and new_rate != representative.commission_percentage:
                return Response({
                    'success': False,
                    'message': 'لا يمكن تعديل نسبة المندوب بعد الموافقة',
                }, status=status.HTTP_400_BAD_REQUEST)

            if update_fields:
                representative.save(update_fields=update_fields)

                # Notify representative
                from .tasks import send_representative_permissions_updated_notification
                send_representative_permissions_updated_notification.delay(representative.id)

            return Response({
                'success': True,
                'message': 'حُدّثت الصلاحيات',
                'data': RepresentativeSerializer(representative, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تحديث الصلاحيات')

    # In RepresentativeViewSet
    @swagger_auto_schema(
        operation_description="Get representative's public purchase link for sharing with buyers",
        responses={200: "Representative purchase link"}
    )
    @action(detail=True, methods=['get'])
    def purchase_link(self, request, pk=None):
        """Get the purchase link for this representative assignment"""
        representative_assignment = self.get_object()

        if representative_assignment.status not in ['accepted', 'active']:
            return Response({
                'success': False,
                'message': 'المندوبية غير نشطة'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Generate the purchase URL
        # This would be: /api/representatives/{id}/create_reservation_for_buyer/
        purchase_url = f"/api/representatives/{representative_assignment.id}/create_reservation_for_buyer/"

        return Response({
            'success': True,
            'data': {
                'representative_id': representative_assignment.id,
                'representative': representative_assignment.representative.username,
                'item_id': representative_assignment.item.id,
                'item_title': representative_assignment.item.title,
                'commission_percentage': str(representative_assignment.commission_percentage),
                'purchase_endpoint': purchase_url,
                'message': 'Share this endpoint with buyers to get commission on their purchases'
            }
        })

    # The generic create/update endpoints are closed. Every change to an
    # assignment goes through an action that checks who is asking:
    # create_request (the representative proposes), accept/reject (only the
    # owner), update_permissions (only the owner), cancel. A plain PATCH let
    # the representative accept their own request and set their commission to
    # 100%, which create_reservation_for_buyer then paid out of the seller's
    # sale.
    def _closed(self, use):
        return Response({
            'success': False,
            'message': f'Use {use} instead',
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def create(self, request, *args, **kwargs):
        return self._closed('/representatives/create_request/')

    def update(self, request, *args, **kwargs):
        return self._closed('accept/, reject/ or update_permissions/')

    def partial_update(self, request, *args, **kwargs):
        return self._closed('accept/, reject/ or update_permissions/')

    def destroy(self, request, *args, **kwargs):
        representative = self.get_object()

        # Only the representative or the owner can delete
        if representative.representative != request.user and representative.owner != request.user:
            return Response({
                'success': False,
                'message': 'الحذف للمندوب أو صاحب المنتج فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        return super().destroy(request, *args, **kwargs)

class ItemTransferViewSet(viewsets.ModelViewSet):
    """
    Wholesale Ownership Transfer:
    - Reseller (to_user) requests a quantity of the owner's (from_user) item
    - On approval, that quantity is deducted from the owner's stock, the
      reseller's wallet is charged a wholesale price, and a new item fully
      owned by the reseller is created
    - Stock physically stays with the owner, who may charge storage fees
    """
    queryset = ItemTransfer.objects.all()
    serializer_class = ItemTransferSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_class = ItemTransferFilter
    search_fields = ['item__title', 'from_user__username', 'to_user__username']
    ordering_fields = ['initiated_at', 'agreement_end_date', 'duration_days']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return ItemTransfer.objects.none()
        user = self.request.user
        return (
            ItemTransfer.objects
            .filter(Q(from_user=user) | Q(to_user=user))
            .select_related('item', 'from_user', 'to_user', 'resulting_item',
                            'item__fixed_listing', 'resulting_item__fixed_listing',
                            'item__auction_listing', 'resulting_item__auction_listing',
                            'from_user__store', 'to_user__store')
            .prefetch_related('storage_fee_payments', 'item__images', 'resulting_item__images',
                              'from_user__store__badges', 'to_user__store__badges')
        )

    def get_serializer_class(self):
        if self.action == 'create_agreement':
            return ResaleAgreementCreateSerializer
        if self.action == 'reject':
            return ResaleRejectSerializer
        return ItemTransferSerializer

    def destroy(self, request, *args, **kwargs):
        # A hard delete here would CASCADE every StorageFeePayment tied to
        # this agreement — real financial records. reject/cancel already
        # end the agreement via status without erasing that history.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف اتفاقية النقل نهائيًا. استخدم reject أو cancel لإنهائها.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # Create and update are closed the same way. Every state change goes
    # through an action that checks who is asking and from which state:
    # create_agreement, approve, reject, cancel, complete. A plain PATCH let
    # the owner rewind an executed agreement to 'pending', raise its quantity
    # and approve it again, charging the reseller's wallet for stock they
    # never asked for, as many times as their balance allowed.
    def _closed(self):
        return Response({
            'success': False,
            'message': 'Use create_agreement/, approve/, reject/, cancel/ or complete/',
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def create(self, request, *args, **kwargs):
        return self._closed()

    def update(self, request, *args, **kwargs):
        return self._closed()

    def partial_update(self, request, *args, **kwargs):
        return self._closed()

    # ==================== WHOLESALE TRANSFER ENDPOINTS ====================

    @action(detail=False, methods=['post'])
    @one_at_a_time('agreement')
    def create_agreement(self, request):
        """Reseller requests a quantity of the owner's item for wholesale ownership transfer"""
        serializer = ResaleAgreementCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({'success': False, 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        v = serializer.validated_data
        item = Item.objects.get(id=v['item_id'])

        with transaction.atomic():
            variant, variant_error = _resolve_variant(item, v.get('variant_id'))
            if variant_error:
                return Response({'success': False, 'message': variant_error},
                                status=status.HTTP_400_BAD_REQUEST)
            if v.get('quantity', 1) > variant.quantity:
                return Response({'success': False,
                                 'message': f'المتوفّر {variant.quantity} فقط'},
                                status=status.HTTP_400_BAD_REQUEST)
            transfer = ItemTransfer.objects.create(
                item=item,
                variant=variant,
                # What the reseller is agreeing to pay — see activate().
                wholesale_price_per_unit=(variant.price if variant.price is not None
                                          else item.fixed_listing.price),
                from_user=item.owner,
                to_user=request.user,
                quantity=v.get('quantity', 1),
                storage_fee_type=v.get('storage_fee_type', 'none'),
                storage_fee_amount=v.get('storage_fee_amount'),
                storage_fee_period_days=v.get('storage_fee_period_days', 30),
                duration_days=v.get('duration_days'),
                request_message=v.get('request_message', ''),
                notes=v.get('notes', ''),
                status='pending',
            )

        from .tasks import send_resale_agreement_request_notification
        send_resale_agreement_request_notification.delay(transfer.id)

        return Response({
            'success': True,
            'message': f'أُرسل طلبك إلى {item.owner.username}',
            'data': ItemTransferSerializer(transfer, context={'request': request}).data
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def pending_approvals(self, request):
        """Pending agreements waiting for the current user (as owner) to approve"""
        qs = self.get_queryset().filter(from_user=request.user, status='pending')
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def my_agreements(self, request):
        """All agreements where current user is the reseller"""
        qs = self.get_queryset().filter(to_user=request.user)
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def active(self, request):
        """Active resale agreements for the current user (owner or reseller)"""
        qs = self.get_queryset().filter(status='active')
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Owner approves — transfers ownership of the quantity immediately at the item's listed price"""
        transfer = self.get_object()
        if transfer.from_user != request.user:
            return Response({'success': False, 'message': 'الموافقة لصاحب المنتج فقط'}, status=status.HTTP_403_FORBIDDEN)
        # Told before they agree, whatever build they are on: the app shows the
        # notice and sends the flag; an older one shows this message, and its
        # next tap goes through.
        if (transfer.status == 'pending' and needs_notice(request.user)
                and str(request.data.get('escrow_ack', '')).lower() not in ('true', '1')):
            mark_notice_seen(request.user)
            return Response({'success': False, 'code': 'escrow_notice', 'message': NOTICE},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                transfer = ItemTransfer.objects.select_for_update().get(pk=transfer.pk)
                transfer.activate()
                if transfer.escrow:
                    mark_notice_seen(request.user)
        except ValueError as e:
            return Response({'success': False, 'message': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        from .tasks import send_resale_agreement_approved_notification
        send_resale_agreement_approved_notification.delay(transfer.id)

        return Response({
            'success': True,
            'message': 'فُعّلت الاتفاقية، وانتقلت الكمية إلى المشتري',
            'data': ItemTransferSerializer(transfer, context={'request': request}).data
        })

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Owner rejects the agreement request"""
        transfer = self.get_object()
        if transfer.from_user != request.user:
            return Response({'success': False, 'message': 'الرفض لصاحب المنتج فقط'}, status=status.HTTP_403_FORBIDDEN)
        if transfer.status != 'pending':
            return Response({'success': False, 'message': 'لا يمكن رفض هذه الاتفاقية'}, status=status.HTTP_400_BAD_REQUEST)
        serializer = ResaleRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            transfer = ItemTransfer.objects.select_for_update().get(pk=transfer.pk)
            if transfer.status != 'pending':
                return Response({'success': False, 'message': 'لا يمكن رفض هذه الاتفاقية'}, status=status.HTTP_400_BAD_REQUEST)
            transfer.status = 'rejected'
            transfer.rejected_at = timezone.now()
            transfer.rejection_reason = serializer.validated_data.get('rejection_reason', '')
            transfer.save(update_fields=['status', 'rejected_at', 'rejection_reason'])

        from .tasks import send_resale_agreement_rejected_notification
        send_resale_agreement_rejected_notification.delay(transfer.id)

        return Response({'success': True, 'message': 'رُفضت الاتفاقية', 'data': ItemTransferSerializer(transfer, context={'request': request}).data})

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Reseller cancels a pending request; owner ends an active storage-hosting agreement (only once its term has ended)"""
        transfer = self.get_object()
        can = (
            (transfer.to_user == request.user and transfer.status == 'pending') or
            (transfer.from_user == request.user and transfer.status == 'active')
        )
        if not can:
            return Response({'success': False, 'message': 'لا يمكن إلغاء هذه الاتفاقية'}, status=status.HTTP_403_FORBIDDEN)
        if transfer.status == 'active' and transfer.held_quantity:
            return self._money_held()
        if transfer.status == 'active' and transfer.from_user == request.user and not transfer.term_has_ended():
            return Response({
                'success': False,
                'message': f'لا تنتهي الاتفاقية قبل {transfer.agreement_end_date:%Y-%m-%d}',
            }, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            # What was cancellable a moment ago may have been approved and
            # paid for since: the decision is made on the locked row.
            seen = transfer.status
            transfer = ItemTransfer.objects.select_for_update().get(pk=transfer.pk)
            if transfer.status != seen:
                return Response({'success': False, 'message': 'تغيّرت حالة الاتفاقية — حدّث الصفحة'}, status=status.HTTP_400_BAD_REQUEST)
            transfer.status = 'cancelled'
            transfer.cancelled_at = timezone.now()
            transfer.save(update_fields=['status', 'cancelled_at'])

        from .tasks import send_resale_agreement_cancelled_notification
        send_resale_agreement_cancelled_notification.delay(transfer.id, request.user.id)

        return Response({'success': True, 'message': 'أُلغيت الاتفاقية', 'data': ItemTransferSerializer(transfer, context={'request': request}).data})

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """
        Close an active agreement. The reseller may end the storage-hosting relationship
        anytime (it's their stock); the owner may only end it once its term has passed
        (or immediately if the agreement is open-ended, i.e. has no agreement_end_date).
        The wholesale ownership transfer already happened at approval and is not reversed —
        this just ends the storage-fee relationship.
        """
        transfer = self.get_object()
        is_owner = transfer.from_user == request.user
        is_reseller = transfer.to_user == request.user
        if not (is_owner or is_reseller):
            return Response({'success': False, 'message': 'لست طرفاً في هذه الاتفاقية'}, status=status.HTTP_403_FORBIDDEN)
        if transfer.status != 'active':
            return Response({'success': False, 'message': 'لا يمكن إغلاق هذه الاتفاقية'}, status=status.HTTP_400_BAD_REQUEST)
        if transfer.held_quantity:
            return self._money_held()
        if is_owner and not transfer.term_has_ended():
            return Response({
                'success': False,
                'message': f'لا تنتهي الاتفاقية قبل {transfer.agreement_end_date:%Y-%m-%d}',
            }, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            transfer = ItemTransfer.objects.select_for_update().get(pk=transfer.pk)
            if transfer.status != 'active':
                return Response({'success': False, 'message': 'لا يمكن إغلاق هذه الاتفاقية'}, status=status.HTTP_400_BAD_REQUEST)
            transfer.status = 'completed'
            transfer.completed_at = timezone.now()
            transfer.save(update_fields=['status', 'completed_at'])
        return Response({
            'success': True,
            'message': 'أُغلقت الاتفاقية',
            'data': ItemTransferSerializer(transfer, context={'request': request}).data
        })

    # ==================== HELD MONEY (services/wholesale.py) ====================

    @staticmethod
    def _money_held():
        return Response({
            'success': False,
            'message': 'ثمن بضاعة لم تُسلَّم ما زال محجوزاً — يؤكّد المشتري الاستلام أو يطلب الاسترداد أولاً',
        }, status=status.HTTP_400_BAD_REQUEST)

    def _held_action(self, request, side, act, message):
        transfer = self.get_object()
        if getattr(transfer, side) != request.user.id:
            return Response({'success': False, 'message': 'هذا الإجراء للطرف الآخر في الاتفاقية'},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            transfer = act(transfer)
        except WholesaleError as exc:
            return Response({'success': False, 'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'success': True, 'message': message,
                         'data': ItemTransferSerializer(transfer, context={'request': request}).data})

    @action(detail=True, methods=['post'])
    def confirm_received(self, request, pk=None):
        """Reseller took goods out of the warehouse: the seller is paid for them."""
        units = request.data.get('quantity')
        try:
            units = None if units in (None, '') else int(units)
        except (TypeError, ValueError):
            return Response({'success': False, 'message': 'الكمية غير صالحة'},
                            status=status.HTTP_400_BAD_REQUEST)
        return self._held_action(request, 'to_user_id', lambda t: confirm_received(t, units),
                                 'أُكّد الاستلام وحُرِّر الثمن للبائع')

    @action(detail=True, methods=['post'])
    def request_refund(self, request, pk=None):
        """Reseller wants back the price of what they never received."""
        return self._held_action(request, 'to_user_id', request_refund,
                                 'أُرسل طلب الاسترداد إلى البائع')

    @action(detail=True, methods=['post'])
    def accept_refund(self, request, pk=None):
        """Seller agrees: unsold pieces come back to stock, their price goes back."""
        def act(transfer):
            if transfer.refund_requested_at is None:
                raise WholesaleError('لا طلب استرداد على هذه الاتفاقية')
            return refund_rest(transfer)
        return self._held_action(request, 'from_user_id', act, 'رُدّ الثمن وعادت القطع إلى مخزونك')

    @action(detail=True, methods=['post'])
    def claim_handover(self, request, pk=None):
        """Seller says they handed the rest over; paid in a week unless disputed."""
        return self._held_action(request, 'from_user_id', claim_handover,
                                 'أُبلغ المشتري — يُحرَّر الثمن إن لم يعترض خلال 7 أيام')

    # ==================== OWNER-SIDE GETS ====================

    @action(detail=False, methods=['get'])
    def items_on_consignment(self, request):
        """Owner: your stock currently held under active wholesale-transfer agreements"""
        qs = self.get_queryset().filter(from_user=request.user, status='active')
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def received_requests(self, request):
        """Owner: pending agreements waiting for your approval"""
        qs = self.get_queryset().filter(from_user=request.user, status='pending')
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def sent_requests(self, request):
        """Reseller: agreements you sent, waiting for owner approval"""
        qs = self.get_queryset().filter(to_user=request.user, status='pending')
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def history(self, request):
        """All completed/cancelled/rejected agreements for the current user"""
        qs = self.get_queryset().filter(status__in=['completed', 'cancelled', 'rejected', 'expired'])
        return Response({'success': True, 'count': qs.count(), 'data': self.get_serializer(qs, many=True).data})

class ChatViewSet(viewsets.ModelViewSet):
    """Chat management"""
    queryset = Chat.objects.all()
    serializer_class = ChatSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_fields = ['item__id', 'is_active']
    search_fields = ['item__title', 'buyer__username', 'seller__username']
    ordering_fields = ['created_at', 'updated_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Chat.objects.none()

        user = self.request.user
        queryset = Chat.objects.filter(
            Q(buyer=user) | Q(seller=user)
        ).select_related('item', 'buyer', 'seller', 'store')
        if self.action == 'list':
            queryset = user_related(item_related(queryset, 'item'), 'buyer', 'seller')

        # Only `list` splits by archived/non-archived (the inbox vs. "الأرشيف"
        # screens). Every detail action — retrieve, send_message, messages,
        # archive/unarchive itself, ... — goes through get_object(), which
        # also calls this method; if an archived chat was excluded here
        # unconditionally, opening one from the archive view would 404.
        if self.action == 'list':
            show_archived = self.request.query_params.get('archived', 'false').lower() == 'true'
            archived_q = (
                Q(buyer=user, is_archived_by_buyer=True) |
                Q(seller=user, is_archived_by_seller=True)
            )
            queryset = queryset.filter(archived_q) if show_archived else queryset.exclude(archived_q)

        # صراحةً بدل الاتكال على Chat.Meta.ordering فقط — الأحدث نشاطاً أولاً
        # (Message.save() يحدّث chat.updated_at في كل رسالة)، حتى تبقى
        # المحادثات النشطة بأعلى القائمة كما تتوقّع شاشة "المحادثات".
        return queryset.order_by('-updated_at')

    def destroy(self, request, *args, **kwargs):
        # A hard delete here would CASCADE every Message in the thread for
        # BOTH parties, not just the caller. archive/unarchive already give
        # each side their own "remove from my inbox" without touching the
        # other party's copy.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف المحادثة نهائيًا. استخدم أرشفة المحادثة (archive) لإخفائها من قائمتك.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ChatDetailSerializer
        elif self.action == 'create_or_get':
            return ChatCreateSerializer
        elif self.action == 'create_or_get_store':
            return StoreChatCreateSerializer
        elif self.action == 'create_or_get_support':
            return SupportChatCreateSerializer
        elif self.action == 'send_message':
            return MessageCreateSerializer
        elif self.action == 'send_offer':
            return MessageOfferSerializer
        return ChatSerializer

    @swagger_auto_schema(
        operation_description="Create or get existing chat for an item",
        request_body=ChatCreateSerializer,
        responses={
            200: ChatDetailSerializer,
            201: ChatDetailSerializer,
            400: "Validation failed"
        }
    )
    @action(detail=False, methods=['post'])
    def create_or_get(self, request):
        """Create a new chat or get existing chat for an item"""
        serializer = ChatCreateSerializer(data=request.data, context={'request': request})

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        item_id = serializer.validated_data['item_id']
        initial_message = serializer.validated_data.get('initial_message', '')

        try:
            item = Item.objects.get(id=item_id)

            # Get or create chat
            chat, created = chat_between(
                item=item,
                buyer=request.user,
                seller=item.owner
            )

            # If chat was archived, unarchive it
            if request.user == chat.buyer and chat.is_archived_by_buyer:
                chat.is_archived_by_buyer = False
                chat.save(update_fields=['is_archived_by_buyer'])
            elif request.user == chat.seller and chat.is_archived_by_seller:
                chat.is_archived_by_seller = False
                chat.save(update_fields=['is_archived_by_seller'])

            # Send initial message if provided and chat is new
            if created and initial_message and not blocked_between(chat.buyer_id, chat.seller_id):
                message = Message.objects.create(
                    chat=chat,
                    sender=request.user,
                    message_type='user',
                    content=initial_message
                )
                sync_message_to_firestore(message)

            sync_chat_to_firestore(chat)

            status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
            return Response({
                'success': True,
                'message': 'فُتحت المحادثة',
                'data': ChatDetailSerializer(chat, context={'request': request}).data
            }, status=status_code)

        except Item.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المنتج غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            return server_error('تعذّر فتح المحادثة')

    @swagger_auto_schema(
        operation_description="Create or get a direct chat with a store",
        request_body=StoreChatCreateSerializer,
        responses={
            200: ChatDetailSerializer,
            201: ChatDetailSerializer,
            400: "Validation failed"
        }
    )
    @action(detail=False, methods=['post'], url_path='create_or_get_store')
    def create_or_get_store(self, request):
        """Create a new store-level chat or get the existing one"""
        serializer = StoreChatCreateSerializer(
            data=request.data, context={'request': request}
        )
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        store_id = serializer.validated_data['store_id']
        initial_message = serializer.validated_data.get('initial_message', '')

        try:
            store = UserStore.objects.get(id=store_id)
            chat, created = chat_between(
                store=store,
                item=None,
                buyer=request.user,
                seller=store.user,
            )

            # If chat was archived, unarchive it
            if request.user == chat.buyer and chat.is_archived_by_buyer:
                chat.is_archived_by_buyer = False
                chat.save(update_fields=['is_archived_by_buyer'])
            elif request.user == chat.seller and chat.is_archived_by_seller:
                chat.is_archived_by_seller = False
                chat.save(update_fields=['is_archived_by_seller'])

            if created and initial_message and not blocked_between(chat.buyer_id, chat.seller_id):
                message = Message.objects.create(
                    chat=chat,
                    sender=request.user,
                    message_type='user',
                    content=initial_message
                )
                sync_message_to_firestore(message)

            sync_chat_to_firestore(chat)

            status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
            return Response({
                'success': True,
                'message': 'فُتحت المحادثة',
                'data': ChatDetailSerializer(chat, context={'request': request}).data
            }, status=status_code)

        except UserStore.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المتجر غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            return server_error('تعذّر فتح المحادثة')

    @swagger_auto_schema(
        operation_description="Create or get the current user's support chat",
        request_body=SupportChatCreateSerializer,
        responses={
            200: ChatDetailSerializer,
            201: ChatDetailSerializer,
        }
    )
    @action(detail=False, methods=['post'], url_path='create_or_get_support')
    def create_or_get_support(self, request):
        """Create a new support chat or get the existing one — every user
        has exactly one, with the fixed support account."""
        serializer = SupportChatCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        initial_message = serializer.validated_data.get('initial_message', '')

        try:
            chat, created = start_support_chat(request.user, initial_message=initial_message)
            status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
            return Response({
                'success': True,
                'message': 'فُتحت المحادثة',
                'data': ChatDetailSerializer(chat, context={'request': request}).data
            }, status=status_code)
        except Exception:
            return server_error('تعذّر فتح محادثة الدعم')

    @swagger_auto_schema(
        operation_description="Send a message in a chat",
        request_body=MessageCreateSerializer,
        responses={
            201: MessageSerializer,
            400: "Validation failed",
            403: "Permission denied"
        }
    )
    @action(detail=True, methods=['post'])
    def block(self, request, pk=None):
        """Stop the other side of this chat from messaging me, anywhere."""
        other = self.get_object().get_other_user(request.user)
        if other.is_staff or other.username == SUPPORT_USERNAME:
            return Response({'success': False, 'message': 'لا يمكن حظر حساب الدعم'},
                            status=status.HTTP_400_BAD_REQUEST)
        UserBlock.objects.get_or_create(blocker=request.user, blocked=other)
        return Response({'success': True, 'message': 'حُظر المستخدم — لن يستطيع مراسلتك'})

    @action(detail=True, methods=['post'])
    def unblock(self, request, pk=None):
        other = self.get_object().get_other_user(request.user)
        UserBlock.objects.filter(blocker=request.user, blocked=other).delete()
        return Response({'success': True, 'message': 'أُلغي الحظر'})

    @action(detail=False, methods=['get'])
    def blocked(self, request):
        """Everyone I blocked, with a chat to open (and unblock) them from."""
        rows = UserBlock.objects.filter(blocker=request.user).select_related('blocked')
        data = []
        for row in rows:
            chat = Chat.objects.filter(
                Q(buyer=request.user, seller=row.blocked) | Q(seller=request.user, buyer=row.blocked)
            ).order_by('-updated_at').first()
            data.append({'chat_id': chat.id if chat else None, 'blocked_at': row.created_at,
                         'username': row.blocked.username,
                         'name': store_name_of(row.blocked) or row.blocked.get_full_name() or row.blocked.username})
        return Response({'success': True, 'data': data})

    @action(detail=True, methods=['post'])
    def send_message(self, request, pk=None):
        """Send a message in a chat"""
        chat = self.get_object()

        # Verify user is part of this chat
        if request.user not in [chat.buyer, chat.seller]:
            return Response({
                'success': False,
                'message': 'لست طرفاً في هذه المحادثة'
            }, status=status.HTTP_403_FORBIDDEN)
        if blocked_between(chat.buyer_id, chat.seller_id):
            return Response({'success': False, 'code': 'blocked', 'message': CANNOT_MESSAGE},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = MessageCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            attachment = serializer.validated_data.get('attachment')
            message = Message.objects.create(
                chat=chat,
                sender=request.user,
                message_type='image' if attachment else 'user',
                content=serializer.validated_data.get('content', ''),
                attachment=attachment
            )

            # Send notification to other user (best-effort; never fail the send)
            try:
                other_user = chat.get_other_user(request.user)
                send_websocket_notification(
                    user_id=other_user.id,
                    notification_type='new_message',
                    title=request.user.username,
                    message=message.content[:120] if message.content else 'أرسل مرفقاً',
                    data={'chat_id': chat.id, 'message_id': message.id}
                )
            except Exception:
                pass

            # الإشعار الفعلي (FCM + بريد) — send_websocket_notification أعلاه
            # يبث فقط إلى ws/notifications/ الذي لا يستمع له تطبيق الجوال
            # حالياً (الإشعارات هناك تعتمد على FCM حصراً)، فبدونه لا يصل أي
            # إشعار لرسائل الصور إطلاقاً.
            try:
                notify_new_message(message)
            except Exception as e:
                logger.error(f"Failed to send push notification for image message: {e}")

            # Push into the live WebSocket group so a connected participant
            # sees it immediately (the socket itself can't carry the upload).
            response_data = MessageSerializer(message, context={'request': request}).data
            try:
                from .consumers import broadcast_chat_message
                broadcast_chat_message(message, attachment_url=response_data.get('attachment_url'))
            except Exception as e:
                logger.error(f"Error broadcasting chat message: {str(e)}")

            return Response({
                'success': True,
                'message': 'أُرسلت الرسالة',
                'data': response_data
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر إرسال الرسالة')

    @swagger_auto_schema(
        operation_description="Send a price offer",
        request_body=MessageOfferSerializer,
        responses={
            201: MessageSerializer,
            400: "Validation failed",
            403: "Permission denied"
        }
    )
    @action(detail=True, methods=['post'])
    def send_offer(self, request, pk=None):
        """Send a price offer in the chat"""
        chat = self.get_object()

        # Only buyer can send offers
        if request.user != chat.buyer:
            return Response({
                'success': False,
                'message': 'العرض للمشتري فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        if blocked_between(chat.buyer_id, chat.seller_id):
            return Response({'success': False, 'code': 'blocked', 'message': CANNOT_MESSAGE},
                            status=status.HTTP_403_FORBIDDEN)

        if not chat.item:
            return Response({
                'success': False,
                'message': 'لا يمكن إرسال عرض سعر إلا في محادثة عن منتج محدد'
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = MessageOfferSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        offer_amount = serializer.validated_data['offer_amount']
        quantity = serializer.validated_data['quantity']
        message_text = serializer.validated_data.get('message', '')

        with transaction.atomic():
            variant, variant_error = _resolve_variant(
                chat.item, serializer.validated_data.get('variant_id'))
        if variant_error:
            return Response({'success': False, 'message': variant_error},
                            status=status.HTTP_400_BAD_REQUEST)

        if quantity > variant.quantity:
            return Response({
                'success': False,
                'message': f'الكمية المتوفرة من هذا المنتج {variant.quantity} فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            content = f"عرض سعر: {offer_amount} د.ع لعدد {quantity}"
            if chat.item.options:
                # The seller answers for what they can see.
                content += ' — ' + ' / '.join(str(x) for x in (variant.attributes or {}).values())
            if message_text:
                content += f"\n{message_text}"

            message = Message.objects.create(
                chat=chat,
                sender=request.user,
                message_type='offer',
                content=content,
                offer_amount=offer_amount,
                offer_quantity=quantity,
                offer_variant=variant if chat.item.options else None,
                offer_status='pending'
            )

            # Push into the live WebSocket group so a connected seller sees
            # the offer bubble immediately (same as send_message)
            try:
                from .consumers import broadcast_chat_message
                broadcast_chat_message(message)
            except Exception as e:
                logger.error(f"Error broadcasting offer message: {str(e)}")

            # Notify seller
            try:
                send_websocket_notification(
                    user_id=chat.seller_id,
                    notification_type='offer_received',
                    title='عرض سعر جديد',
                    message=f'{request.user.username} عرض {offer_amount} على "{chat.item.title}".',
                    data={'chat_id': chat.id, 'message_id': message.id}
                )
            except Exception as e:
                logger.error(f"Failed to send offer notification: {e}", exc_info=True)

            # الإشعار الفعلي (FCM + بريد) — انظر التعليق في send_message أعلاه
            try:
                notify_new_message(message)
            except Exception as e:
                logger.error(f"Failed to send push notification for offer message: {e}")

            return Response({
                'success': True,
                'message': 'أُرسل العرض',
                'data': MessageSerializer(message, context={'request': request}).data
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر إرسال العرض')

    @swagger_auto_schema(
        operation_description="Mark all messages as read",
        responses={200: "Messages marked as read"}
    )
    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark all messages in chat as read"""
        chat = self.get_object()

        updated_count = chat.messages.exclude(sender=request.user).filter(
            is_read=False
        ).update(is_read=True)

        return Response({
            'success': True,
            'message': f'{updated_count} messages marked as read'
        })

    @swagger_auto_schema(
        operation_description="Fetch messages older than `before` (a message id) for this chat",
        responses={200: "Older messages"}
    )
    @action(detail=True, methods=['get'])
    def messages(self, request, pk=None):
        """Older-messages pagination for a chat thread.

        ChatDetailSerializer (GET /chats/{id}/) only returns the latest
        MESSAGES_PAGE_SIZE messages so a long-running conversation doesn't
        ship its entire history in one response; the client calls this
        action as the user scrolls up, passing the oldest currently-loaded
        message's id as `before` to fetch the page just above it.
        """
        chat = self.get_object()

        qs = chat.messages.all()
        if request.user == chat.buyer:
            qs = qs.filter(is_deleted_by_receiver=False)
        elif request.user == chat.seller:
            qs = qs.exclude(sender=chat.buyer, is_deleted_by_receiver=True)
        # A message the viewer sent and then deleted (destroy() sets
        # is_deleted_by_sender) must stay hidden from *their own* view on
        # every subsequent fetch, regardless of buyer/seller role above.
        qs = qs.exclude(sender=request.user, is_deleted_by_sender=True)

        before = request.query_params.get('before')
        if before:
            try:
                before_message = chat.messages.get(id=before)
            except Message.DoesNotExist:
                return Response({
                    'success': False,
                    'message': 'طلب غير صالح'
                }, status=status.HTTP_400_BAD_REQUEST)
            # Two messages can share a timestamp (an offer and its system
            # note, a fast sender): with `<` alone the one beside the cursor
            # never loaded, and with no tiebreak a page came back shuffled.
            qs = qs.filter(Q(created_at__lt=before_message.created_at)
                           | Q(created_at=before_message.created_at, id__lt=before_message.id))

        page_size = MESSAGES_PAGE_SIZE
        older = list(qs.order_by('-created_at', '-id')[:page_size])
        older.reverse()  # صعوداً زمنياً، مطابقةً لبقية سجل المحادثة

        return Response({
            'success': True,
            'data': MessageSerializer(older, many=True, context={'request': request}).data,
            'has_more': len(older) == page_size,
        })

    @swagger_auto_schema(
        operation_description="Archive a chat",
        responses={200: ChatSerializer}
    )
    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Archive a chat"""
        chat = self.get_object()

        if request.user == chat.buyer:
            chat.is_archived_by_buyer = True
            chat.save(update_fields=['is_archived_by_buyer'])
        elif request.user == chat.seller:
            chat.is_archived_by_seller = True
            chat.save(update_fields=['is_archived_by_seller'])

        return Response({
            'success': True,
            'message': 'أُرشفت المحادثة',
            'data': ChatSerializer(chat, context={'request': request}).data
        })

    @swagger_auto_schema(
        operation_description="Unarchive a chat",
        responses={200: ChatSerializer}
    )
    @action(detail=True, methods=['post'])
    def unarchive(self, request, pk=None):
        """Unarchive a chat"""
        chat = self.get_object()

        if request.user == chat.buyer:
            chat.is_archived_by_buyer = False
            chat.save(update_fields=['is_archived_by_buyer'])
        elif request.user == chat.seller:
            chat.is_archived_by_seller = False
            chat.save(update_fields=['is_archived_by_seller'])

        return Response({
            'success': True,
            'message': 'أُعيدت المحادثة',
            'data': ChatSerializer(chat, context={'request': request}).data
        })

    @swagger_auto_schema(
        operation_description="Get chat statistics",
        responses={200: "Chat statistics"}
    )
    @action(detail=False, methods=['get'])
    def statistics(self, request):
        """Get chat statistics for current user"""
        user = request.user

        total_chats = Chat.objects.filter(
            Q(buyer=user) | Q(seller=user)
        ).count()

        unread_chats = Chat.objects.filter(
            Q(buyer=user) | Q(seller=user)
        ).annotate(
            unread_count=Count(
                'messages',
                filter=Q(messages__is_read=False) & ~Q(messages__sender=user)
            )
        ).filter(unread_count__gt=0).count()

        total_unread_messages = Message.objects.filter(
            Q(chat__buyer=user) | Q(chat__seller=user),
            is_read=False
        ).exclude(sender=user).count()

        archived_chats = Chat.objects.filter(
            Q(buyer=user, is_archived_by_buyer=True) |
            Q(seller=user, is_archived_by_seller=True)
        ).count()

        return Response({
            'success': True,
            'data': {
                'total_chats': total_chats,
                'unread_chats': unread_chats,
                'total_unread_messages': total_unread_messages,
                'archived_chats': archived_chats
            }
        })


class MessageViewSet(viewsets.ModelViewSet):
    """Message management"""
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_fields = ['chat', 'is_read', 'message_type']
    ordering_fields = ['created_at']
    # 'post' has to stay allowed so the accept_offer/reject_offer actions
    # below are reachable — direct message creation is blocked by overriding
    # create() instead, not by removing 'post' from http_method_names.
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Message.objects.none()

        user = self.request.user
        return Message.objects.filter(
            Q(chat__buyer=user) | Q(chat__seller=user)
        ).select_related('chat', 'sender')

    def create(self, request, *args, **kwargs):
        """Direct message creation isn't supported — use chat.send_message"""
        return Response({
            'success': False,
            'message': 'Use /chats/{id}/send_message/ to send a message'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @swagger_auto_schema(
        operation_description="Edit a message (within 15 minutes)",
        responses={200: MessageSerializer, 400: "Cannot edit", 403: "Permission denied"}
    )
    def partial_update(self, request, pk=None):
        """Edit a message"""
        message = self.get_object()

        # Only sender can edit
        if message.sender != request.user:
            return Response({
                'success': False,
                'message': 'التعديل لمرسل الرسالة فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Can only edit user messages
        if message.message_type != 'user':
            return Response({
                'success': False,
                'message': 'لا يمكن تعديل هذه الرسالة'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Can only edit within 15 minutes
        time_limit = timezone.now() - timezone.timedelta(minutes=15)
        if message.created_at < time_limit:
            return Response({
                'success': False,
                'message': 'تُعدّل الرسالة خلال 15 دقيقة فقط'
            }, status=status.HTTP_400_BAD_REQUEST)

        if 'content' in request.data:
            content = str(request.data['content'] or '').strip()
            if not content or len(content) > MessageCreateSerializer.MAX_LENGTH:
                return Response({'success': False, 'message': 'نص الرسالة فارغ أو طويل جداً'},
                                status=status.HTTP_400_BAD_REQUEST)
            message.content = content
            message.edited_at = timezone.now()
            message.save(update_fields=['content', 'edited_at'])

        return Response({
            'success': True,
            'message': 'عُدّلت الرسالة',
            'data': MessageSerializer(message, context={'request': request}).data
        })

    @swagger_auto_schema(
        operation_description="Delete a message",
        responses={200: "Message deleted", 403: "Permission denied"}
    )
    def destroy(self, request, pk=None):
        """Delete a message (soft delete)"""
        message = self.get_object()

        # Only sender can delete
        if message.sender != request.user:
            return Response({
                'success': False,
                'message': 'الحذف لمرسل الرسالة فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        message.is_deleted_by_sender = True
        message.save(update_fields=['is_deleted_by_sender'])

        return Response({
            'success': True,
            'message': 'حُذفت الرسالة'
        })

    @swagger_auto_schema(
        operation_description="Accept a price offer (seller only)",
        responses={200: MessageSerializer, 400: "Cannot accept", 403: "Permission denied"}
    )
    @action(detail=True, methods=['post'])
    def accept_offer(self, request, pk=None):
        """Seller accepts a price offer"""
        message = self.get_object()

        # Only seller can accept
        if message.chat.seller != request.user:
            return Response({
                'success': False,
                'message': 'قبول العرض للبائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Must be an offer
        if message.message_type not in ['offer', 'counter_offer']:
            return Response({
                'success': False,
                'message': 'هذه ليست رسالة عرض'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Must be pending
        if message.offer_status != 'pending':
            return Response({
                'success': False,
                'message': 'حُسم هذا العرض مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                message.offer_status = 'accepted'
                message.offer_accepted_at = timezone.now()
                message.save(update_fields=['offer_status', 'offer_accepted_at'])

                # Create system message
                system_message = Message.objects.create(
                    chat=message.chat,
                    sender=None,
                    message_type='system',
                    content=f"قَبِل {request.user.username} العرض {message.offer_amount} د.ع"
                )

            try:
                from .consumers import broadcast_chat_message
                broadcast_chat_message(system_message)
            except Exception as e:
                logger.error(f"Error broadcasting offer-accepted message: {str(e)}")

            # Notify buyer
            offer_accepted_title = 'تم قبول عرضك'
            offer_accepted_body = (
                f'وافق البائع على عرضك بقيمة {message.offer_amount} '
                f'لمنتج "{bidi_isolate(message.chat.item.title) if message.chat.item else ""}".'
            )
            try:
                send_websocket_notification(
                    user_id=message.chat.buyer_id,
                    notification_type='offer_accepted',
                    title=offer_accepted_title,
                    message=offer_accepted_body,
                    data={'chat_id': message.chat_id, 'message_id': message.id}
                )
            except Exception as e:
                logger.error(f"Failed to send offer accepted notification: {e}", exc_info=True)

            # الإشعار الفعلي (FCM + بريد) — انظر التعليق في send_message أعلاه.
            # system_message مُرسلها None فلا يمكن استخدام notify_new_message
            # مباشرة (تفترض مُرسلاً حقيقياً)، لذا نستدعي notify_push مباشرة.
            try:
                notify_push(
                    message.chat.buyer, offer_accepted_title, offer_accepted_body,
                    data={
                        'type': 'new_message',
                        'chat_id': str(message.chat_id),
                        'content': system_message.content,
                        'msg_type': system_message.message_type,
                        'created_at': system_message.created_at.isoformat(),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to send offer accepted push: {e}")

            return Response({
                'success': True,
                'message': 'قُبل العرض',
                'data': MessageSerializer(message, context={'request': request}).data
            })

        except Exception:
            return server_error('تعذّر قبول العرض')

    @swagger_auto_schema(
        operation_description="Reject a price offer (seller only)",
        responses={200: MessageSerializer, 400: "Cannot reject", 403: "Permission denied"}
    )
    @action(detail=True, methods=['post'])
    def reject_offer(self, request, pk=None):
        """Seller rejects a price offer"""
        message = self.get_object()

        # Only seller can reject
        if message.chat.seller != request.user:
            return Response({
                'success': False,
                'message': 'رفض العرض للبائع فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        # Must be an offer
        if message.message_type not in ['offer', 'counter_offer']:
            return Response({
                'success': False,
                'message': 'هذه ليست رسالة عرض'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Must be pending
        if message.offer_status != 'pending':
            return Response({
                'success': False,
                'message': 'حُسم هذا العرض مسبقاً'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            message.offer_status = 'rejected'
            message.save(update_fields=['offer_status'])

            # Create system message
            system_message = Message.objects.create(
                chat=message.chat,
                sender=None,
                message_type='system',
                content=f"Offer of {message.offer_amount} was rejected"
            )

            try:
                from .consumers import broadcast_chat_message
                broadcast_chat_message(system_message)
            except Exception as e:
                logger.error(f"Error broadcasting offer-rejected message: {str(e)}")

            # الإشعار الفعلي (FCM + بريد) للمشتري — لم يكن هناك أي إشعار على
            # الإطلاق عند رفض العرض (بعكس القبول أعلاه).
            try:
                notify_push(
                    message.chat.buyer,
                    'تم رفض عرضك',
                    f'رفض البائع عرضك بقيمة {message.offer_amount} '
                    f'لمنتج "{bidi_isolate(message.chat.item.title) if message.chat.item else ""}".',
                    data={
                        'type': 'new_message',
                        'chat_id': str(message.chat_id),
                        'content': system_message.content,
                        'msg_type': system_message.message_type,
                        'created_at': system_message.created_at.isoformat(),
                    },
                )
            except Exception as e:
                logger.error(f"Failed to send offer rejected push: {e}")

            return Response({
                'success': True,
                'message': 'رُفض العرض',
                'data': MessageSerializer(message, context={'request': request}).data
            })

        except Exception:
            return server_error('تعذّر رفض العرض')


class UserStoreViewSet(viewsets.ModelViewSet):
    """
    Manage user stores/profiles

    Features:
    - Create personal store
    - Update store details
    - View own store
    - Browse other users' stores
    - Manage store settings
    """
    queryset = UserStore.objects.all()
    serializer_class = UserStoreSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        """
        Public can view stores
        Only authenticated users can create/update
        """
        if self.action in ['list', 'retrieve', 'public_store', 'store_items']:
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        """Filter stores based on action"""
        if getattr(self, 'swagger_fake_view', False):
            return UserStore.objects.none()

        # Public views - only show active stores
        if self.action in ['list', 'retrieve', 'public_store']:
            stores = UserStore.objects.filter(
                is_active=True, user__is_active=True).select_related('user', 'default_location')
            if self.action == 'list':
                stores = with_store_numbers(
                    stores.select_related('default_location__user__store'), self.request.user)
            return stores

        # User's own stores
        if self.request.user.is_authenticated:
            return UserStore.objects.filter(user=self.request.user).select_related('user', 'default_location')

        return UserStore.objects.none()

    def get_serializer_class(self):
        if self.action == 'create':
            return UserStoreCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return UserStoreUpdateSerializer
        return UserStoreSerializer

    def destroy(self, request, *args, **kwargs):
        # A hard delete here would CASCADE every StoreReview, StoreFollow,
        # StoreCoupon (+ CouponCollection), LiveSession (+ its items/likes/
        # comments) and Chat tied to this store. toggle_active already gives
        # the owner a reversible "close my store" without wiping that history.
        return Response({
            'success': False,
            'message': 'لا يمكن حذف المتجر نهائيًا. استخدم toggle_active لإغلاق المتجر مؤقتًا.'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @swagger_auto_schema(
        operation_description="""
        Create your personal store/profile

        This is typically called once when user first wants to sell items.
        Each user can only have one store.
        """,
        request_body=UserStoreCreateSerializer,
        responses={
            201: UserStoreSerializer,
            400: "Validation failed or store already exists",
            401: "Authentication required"
        }
    )
    def create(self, request, *args, **kwargs):
        """
        Create user's store

        Required fields:
        - name: Store name (e.g., "John's Electronics")
        - description: Store description (optional)
        - default_location_id: Default location for items (optional)
        """
        # Check if user already has a store
        if hasattr(request.user, 'store'):
            return Response({
                'success': False,
                'message': 'لديك متجر مسبقاً',
                'data': UserStoreSerializer(request.user.store).data
            }, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # Get default location if provided
                default_location = None
                location_id = request.data.get('default_location_id')

                if location_id:
                    try:
                        default_location = Location.objects.get(
                            id=location_id,
                            user=request.user, is_active=True
                        )
                    except (Location.DoesNotExist, TypeError, ValueError):
                        return Response({
                            'success': False,
                            'message': 'العنوان غير موجود'
                        }, status=status.HTTP_400_BAD_REQUEST)

                # Create store
                store = UserStore.objects.create(
                    user=request.user,
                    name=serializer.validated_data['name'],
                    description=serializer.validated_data.get('description', ''),
                    logo=serializer.validated_data.get('logo'),
                    cover_image=serializer.validated_data.get('cover_image'),
                    default_location=default_location,
                    phone_number=serializer.validated_data.get('phone_number', ''),
                    whatsapp_number=serializer.validated_data.get('whatsapp_number', ''),
                    is_active=True
                )

            return Response({
                'success': True,
                'message': 'أُنشئ متجرك، وتستطيع عرض منتجاتك الآن',
                'data': UserStoreSerializer(store).data
            }, status=status.HTTP_201_CREATED)

        except Exception:
            return server_error('تعذّر إنشاء المتجر')

    @swagger_auto_schema(
        operation_description="Update your store details",
        request_body=UserStoreUpdateSerializer,
        responses={
            200: UserStoreSerializer,
            400: "Validation failed",
            403: "Not your store",
            404: "Store not found"
        }
    )
    def update(self, request, *args, **kwargs):
        """Update store details"""
        store = self.get_object()

        # Ensure user owns this store
        if store.user != request.user:
            return Response({
                'success': False,
                'message': 'تعدّل متجرك فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        serializer = self.get_serializer(store, data=request.data, partial=True)

        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                updated_store = serializer.save()

            return Response({
                'success': True,
                'message': 'حُدّث المتجر',
                'data': UserStoreSerializer(updated_store).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تحديث المتجر')

    @swagger_auto_schema(
        operation_description="Get your own store details",
        responses={
            200: UserStoreSerializer,
            404: "Store not found - you need to create one first"
        }
    )
    @action(detail=False, methods=['get'])
    def my_store(self, request):
        """Get current user's store"""
        try:
            store = request.user.store
            return Response({
                'success': True,
                'data': UserStoreSerializer(store, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except UserStore.DoesNotExist:
            return Response({
                'success': False,
                'message': 'ليس لديك متجر بعد — أنشئ واحداً لتبدأ البيع',
                'need_to_create': True
            }, status=status.HTTP_404_NOT_FOUND)

    @swagger_auto_schema(
        operation_description="View a public store by username",
        manual_parameters=[
            openapi.Parameter(
                'username',
                openapi.IN_PATH,
                description="Username of the store owner",
                type=openapi.TYPE_STRING,
                required=True
            )
        ],
        responses={
            200: UserStoreSerializer,
            404: "Store not found"
        }
    )
    @action(detail=False, methods=['get'], url_path='public/(?P<username>[^/.]+)')
    def public_store(self, request, username=None):
        """
        View a public store by username
        Anyone can view active stores
        """
        try:
            user = User.objects.get(username=username)
            store = UserStore.objects.get(user=user, is_active=True, user__is_active=True)

            return Response({
                'success': True,
                'data': UserStoreSerializer(store, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المستخدم غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)

        except UserStore.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المتجر غير موجود أو غير مفعّل'
            }, status=status.HTTP_404_NOT_FOUND)

    @swagger_auto_schema(
        operation_description="Get all items in a store",
        manual_parameters=[
            openapi.Parameter(
                'username',
                openapi.IN_PATH,
                description="Username of the store owner",
                type=openapi.TYPE_STRING,
                required=True
            ),
            openapi.Parameter(
                'include_transferred',
                openapi.IN_QUERY,
                description="Include items being resold (current_holder items)",
                type=openapi.TYPE_BOOLEAN,
                required=False,
                default=True
            )
        ],
        responses={
            200: ItemSerializer(many=True),
            404: "Store not found"
        }
    )
    @action(detail=False, methods=['get'], url_path='public/(?P<username>[^/.]+)/items')
    def store_items(self, request, username=None):
        """
        Get all items available in a store

        Includes:
        - Items owned by the store owner
        - Items being resold by the store owner (if include_transferred=true)
        - Items the store owner represents as an accepted/active representative
          (can_list=True), so their sales agent items show up in their own
          storefront alongside items they own
        """
        try:
            user = User.objects.get(username=username)
            store = UserStore.objects.get(user=user, is_active=True, user__is_active=True)

            include_transferred = request.query_params.get('include_transferred', 'true').lower() == 'true'

            represented_item_ids = Representative.objects.filter(
                representative=user,
                status__in=['accepted', 'active'],
                can_list=True,
            ).values_list('item_id', flat=True)

            visibility_filter = Q(owner=user) | Q(id__in=represented_item_ids)
            if include_transferred:
                visibility_filter |= Q(current_holder=user)

            items = Item.objects.filter(
                status__in=['active', 'reserved']
            ).filter(visibility_filter).filter(
                Q(fixed_listing__isnull=False) | Q(auction_listing__isnull=False)
            ).order_by('-id')
            total = items.count()
            items, next_page = _page_of(request, item_related(items), 100, 100)

            representative_by_item_id = {
                rep.item_id: rep for rep in Representative.objects.filter(
                    representative=user,
                    status__in=['accepted', 'active'],
                    can_list=True,
                    item__in=[i.pk for i in items],
                ).select_related('representative').prefetch_related('representative__store__badges')
            }

            return Response({
                'success': True,
                'count': total,
                'next_page': next_page,
                'store': {
                    'name': store.name,
                    'description': store.description,
                    'owner': user.username
                },
                'data': ItemSerializer(
                    items, many=True,
                    context={'request': request, 'representative_by_item_id': representative_by_item_id}
                ).data
            }, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المستخدم غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)

        except UserStore.DoesNotExist:
            return Response({
                'success': False,
                'message': 'المتجر غير موجود أو غير مفعّل'
            }, status=status.HTTP_404_NOT_FOUND)

    # ─────────────────────────────────────────────────────────────
    # Follow / Unfollow (متابعة المتجر)
    # ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def follow(self, request, pk=None):
        """Follow this store"""
        store = get_object_or_404(UserStore, pk=pk)
        if store.user == request.user:
            return Response({
                'success': False,
                'message': 'لا يمكنك متابعة متجرك الخاص'
            }, status=status.HTTP_400_BAD_REQUEST)
        StoreFollow.objects.get_or_create(follower=request.user, store=store)

        # Weak interest signal: following a store leans the follower toward
        # whatever that store actually sells. Capped at 5 distinct categories
        # so a large store can't cause an unbounded loop here.
        store_categories = Category.objects.filter(
            item__owner=store.user, item__status__in=['active', 'reserved']
        ).distinct()[:5]
        for category in store_categories:
            record_interest(request.user, category, weight=1)

        return Response({
            'success': True,
            'message': 'تمت المتابعة',
            'is_following': True,
            'followers_count': store.followers.count(),
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def unfollow(self, request, pk=None):
        """Unfollow this store"""
        store = get_object_or_404(UserStore, pk=pk)
        StoreFollow.objects.filter(follower=request.user, store=store).delete()
        return Response({
            'success': True,
            'message': 'تم إلغاء المتابعة',
            'is_following': False,
            'followers_count': store.followers.count(),
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='following-items',
            permission_classes=[IsAuthenticated])
    def following_items(self, request):
        """Items from stores the current user follows (home feed)"""
        followed_user_ids = list(StoreFollow.objects.filter(
            follower=request.user
        ).values_list('store__user_id', flat=True))

        items = list(item_related(Item.objects.filter(open_seller_q('owner'))).filter(
            status__in=['active', 'reserved']
        ).filter(
            Q(owner_id__in=followed_user_ids) |
            Q(current_holder_id__in=followed_user_ids)
        ).filter(
            Q(fixed_listing__isnull=False) | Q(auction_listing__isnull=False)
        ).order_by('-id')[:50])

        return Response({
            'success': True,
            'count': len(items),
            'data': ItemSerializer(items, many=True, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='following',
            permission_classes=[IsAuthenticated])
    def following(self, request):
        """Stores the current user follows, each with a preview of its items (view-all screen)"""
        follows = StoreFollow.objects.filter(
            follower=request.user, store__is_active=True
        ).select_related('store', 'store__user').order_by('-created_at')[:30]

        result = []
        for follow in follows:
            store = follow.store
            items = item_related(Item.objects.filter(open_seller_q('owner'))).filter(
                status__in=['active', 'reserved']
            ).filter(
                Q(owner=store.user) | Q(current_holder=store.user)
            ).filter(
                Q(fixed_listing__isnull=False) | Q(auction_listing__isnull=False)
            ).order_by('-id')[:12]

            result.append({
                'store': UserStoreSerializer(store, context={'request': request}).data,
                'items': ItemSerializer(items, many=True, context={'request': request}).data,
            })

        return Response({
            'success': True,
            'count': len(result),
            'data': result,
        }, status=status.HTTP_200_OK)

    # ─────────────────────────────────────────────────────────────
    # Store coupons (كوبونات المتجر)
    # ─────────────────────────────────────────────────────────────
    @action(detail=True, methods=['get', 'post'], permission_classes=[IsAuthenticated],
            parser_classes=[JSONParser, MultiPartParser, FormParser])
    def coupons(self, request, pk=None):
        """
        GET: list coupons for this store (owner sees all incl. inactive/expired
        for management; everyone else sees only active, non-expired coupons).

        POST: create a new coupon for this store (owner only).
        """
        store = get_object_or_404(UserStore, pk=pk)
        is_owner = request.user.is_authenticated and store.user == request.user

        if request.method == 'POST':
            if not is_owner:
                return Response({
                    'success': False,
                    'message': 'يمكنك فقط إضافة كوبونات لمتجرك الخاص'
                }, status=status.HTTP_403_FORBIDDEN)

            serializer = StoreCouponSerializer(
                data=request.data, context={'request': request}
            )
            if not serializer.is_valid():
                return Response({
                    'success': False,
                    'message': 'بيانات غير صالحة',
                    'errors': serializer.errors
                }, status=status.HTTP_400_BAD_REQUEST)

            try:
                coupon = serializer.save(store=store)
            except IntegrityError:
                return Response({
                    'success': False,
                    'message': 'يوجد كوبون بنفس الكود مسبقاً في متجرك'
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                'success': True,
                'message': 'تم إنشاء الكوبون بنجاح',
                'data': StoreCouponSerializer(
                    coupon, context={'request': request}
                ).data
            }, status=status.HTTP_201_CREATED)

        # GET
        if is_owner:
            coupons = store.coupons.all()
        else:
            coupons = [c for c in store.coupons.filter(is_active=True) if c.is_valid]
        return Response({
            'success': True,
            'count': len(coupons),
            'data': StoreCouponSerializer(
                coupons, many=True, context={'request': request}
            ).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['patch', 'delete'],
            url_path='coupons/(?P<coupon_id>[^/.]+)',
            permission_classes=[IsAuthenticated],
            parser_classes=[JSONParser, MultiPartParser, FormParser])
    def coupon_detail(self, request, pk=None, coupon_id=None):
        """Update or delete one of your store's coupons (owner only)"""
        store = get_object_or_404(UserStore, pk=pk)
        if store.user != request.user:
            return Response({
                'success': False,
                'message': 'يمكنك فقط تعديل كوبونات متجرك'
            }, status=status.HTTP_403_FORBIDDEN)

        coupon = get_object_or_404(StoreCoupon, pk=coupon_id, store=store)

        if request.method == 'DELETE':
            # Soft-delete: CouponCollection CASCADEs from StoreCoupon, so a
            # hard delete would silently wipe every customer's "collected"
            # record for it. is_active=False just stops it being usable/listed.
            coupon.is_active = False
            coupon.save(update_fields=['is_active'])
            return Response({
                'success': True,
                'message': 'تم حذف الكوبون'
            }, status=status.HTTP_200_OK)

        serializer = StoreCouponSerializer(
            coupon, data=request.data, partial=True,
            context={'request': request}
        )
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            serializer.save()
        except IntegrityError:
            return Response({
                'success': False,
                'message': 'يوجد كوبون بنفس الكود مسبقاً في متجرك'
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'success': True,
            'message': 'تم تحديث الكوبون',
            'data': StoreCouponSerializer(
                coupon, context={'request': request}
            ).data
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'],
            url_path='coupons/(?P<coupon_id>[^/.]+)/collect',
            permission_classes=[IsAuthenticated])
    def collect_coupon(self, request, pk=None, coupon_id=None):
        """Collect a coupon from this store (جمع الكوبون)"""
        store = get_object_or_404(UserStore, pk=pk)
        try:
            coupon = store.coupons.get(id=coupon_id, is_active=True)
        except StoreCoupon.DoesNotExist:
            return Response({
                'success': False, 'message': 'الكوبون غير متوفّر'
            }, status=status.HTTP_404_NOT_FOUND)
        if not coupon.is_valid:
            return Response({
                'success': False, 'message': 'انتهت صلاحية الكوبون'
            }, status=status.HTTP_400_BAD_REQUEST)
        CouponCollection.objects.get_or_create(user=request.user, coupon=coupon)
        return Response({
            'success': True,
            'message': 'تم جمع الكوبون',
            'data': StoreCouponSerializer(coupon, context={'request': request}).data
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated])
    def my_coupons(self, request):
        """كوبوناتي — الكوبونات التي جمعتها من كل المتاجر"""
        collections = CouponCollection.objects.filter(
            user=request.user
        ).select_related('coupon', 'coupon__store', 'used_in_reservation')
        return Response({
            'success': True,
            'count': collections.count(),
            'data': CouponCollectionSerializer(
                collections, many=True, context={'request': request}
            ).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Activate or deactivate your store",
        manual_parameters=[
            openapi.Parameter(
                'is_active',
                openapi.IN_FORM,
                type=openapi.TYPE_BOOLEAN,
                required=True,
                description='Set to true to activate, false to deactivate'
            )
        ],
        responses={
            200: UserStoreSerializer,
            400: "Invalid request",
            403: "Not your store"
        }
    )
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Activate or deactivate store"""
        store = self.get_object()

        if store.user != request.user:
            return Response({
                'success': False,
                'message': 'تعدّل متجرك فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        is_active = request.data.get('is_active')

        if is_active is None:
            return Response({
                'success': False,
                'message': 'is_active field is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        # bool('false') is True: a form-encoded "false" opened the store.
        is_active = str(is_active).strip().lower() in ('1', 'true', 'yes', 'on')
        try:
            if is_active:
                store.is_active = True
                store.save(update_fields=['is_active'])
            else:
                from .services.moderation import close_store
                close_store(store)

            status_text = 'activated' if is_active else 'deactivated'

            return Response({
                'success': True,
                'message': f'Store {status_text} successfully',
                'data': UserStoreSerializer(store).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تحديث حالة المتجر')

    @swagger_auto_schema(
        operation_description="Update store logo - Use multipart/form-data to upload image file",
        manual_parameters=[
            openapi.Parameter(
                'logo',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='Upload logo image file'
            )
        ],
        responses={
            200: UserStoreSerializer,
            400: "No logo provided",
            403: "Not your store"
        }
    )
    @action(
        detail=True,
        methods=['post'],
        parser_classes=[MultiPartParser, FormParser]
    )
    def update_logo(self, request, pk=None):
        """Upload or update store logo"""
        store = self.get_object()

        if store.user != request.user:
            return Response({
                'success': False,
                'message': 'تعدّل متجرك فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        logo = request.FILES.get('logo')

        if not logo:
            return Response({
                'success': False,
                'message': 'لم تُرفق صورة'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_image(logo)
        except DjangoValidationError as exc:
            return Response({'success': False, 'message': exc.messages[0]},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            # Delete old logo if exists
            if store.logo:
                store.logo.delete(save=False)

            store.logo = logo
            store.save(update_fields=['logo'])

            return Response({
                'success': True,
                'message': 'حُدّث شعار المتجر',
                'data': UserStoreSerializer(store).data
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر تحديث الشعار')

    @swagger_auto_schema(
        operation_description="Set default location for store",
        manual_parameters=[
            openapi.Parameter(
                'location_id',
                openapi.IN_FORM,
                type=openapi.TYPE_INTEGER,
                required=True,
                description='ID of the location to set as default'
            )
        ],
        responses={
            200: UserStoreSerializer,
            400: "Invalid location",
            403: "Not your store",
            404: "Location not found"
        }
    )
    @action(detail=True, methods=['post'])
    def set_default_location(self, request, pk=None):
        """Set the default location for this store"""
        store = self.get_object()

        if store.user != request.user:
            return Response({
                'success': False,
                'message': 'تعدّل متجرك فقط'
            }, status=status.HTTP_403_FORBIDDEN)

        location_id = request.data.get('location_id')

        if not location_id:
            return Response({
                'success': False,
                'message': 'اختر العنوان'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            location = Location.objects.get(id=location_id, user=request.user, is_active=True)

            store.default_location = location
            store.save(update_fields=['default_location'])

            return Response({
                'success': True,
                'message': f'العنوان الرئيسي: {location.name}',
                'data': UserStoreSerializer(store).data
            }, status=status.HTTP_200_OK)

        except (Location.DoesNotExist, TypeError, ValueError):
            return Response({
                'success': False,
                'message': 'العنوان غير موجود'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            return server_error('تعذّر حفظ العنوان')

    @swagger_auto_schema(
        operation_description="Get store statistics",
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """Get store statistics"""
        store = self.get_object()

        # Only owner or public stats
        is_owner = request.user.is_authenticated and store.user == request.user

        try:
            from django.db.models import Q, Count, Sum, Avg

            # Items stats
            owned_items = Item.objects.filter(owner=store.user)
            active_items = owned_items.filter(status='active').count()
            sold_items = owned_items.filter(status='sold').count()

            # Items being resold
            reselling_items = Item.objects.filter(
                current_holder=store.user,
                status='active'
            ).exclude(owner=store.user).count()

            # Reservations stats (only for owner)
            reservations_data = {}
            if is_owner:
                from .models import Reservation
                reservations = Reservation.objects.filter(item__owner=store.user)
                reservations_data = {
                    'total_reservations': reservations.count(),
                    'completed_reservations': reservations.filter(status='completed').count(),
                    'pending_reservations': reservations.filter(status='pending').count(),
                }

            # Transfer stats (only for owner)
            transfer_data = {}
            if is_owner:
                from .models import ItemTransfer

                # Wholesale transfers where this user acquired stock (approved or later)
                bought_for_resale = ItemTransfer.objects.filter(
                    to_user=store.user,
                    status__in=['active', 'completed'],
                )

                # Wholesale transfers where this user sold stock to a reseller
                sold_for_resale = ItemTransfer.objects.filter(
                    from_user=store.user,
                    status__in=['active', 'completed'],
                )

                transfer_data = {
                    'items_bought_for_resale': bought_for_resale.count(),
                    'items_sold_for_resale': sold_for_resale.count(),
                    'items_resold_to_customers': bought_for_resale.filter(
                        resulting_item__status='sold'
                    ).count(),
                }

            stats = {
                'store_name': store.name,
                'is_active': store.is_active,
                'items': {
                    'owned_active': active_items,
                    'owned_sold': sold_items,
                    'reselling_for_others': reselling_items,
                    'total_in_store': active_items + reselling_items
                },
                'created_at': store.created_at,
            }

            # Add owner-only stats
            if is_owner:
                stats['reservations'] = reservations_data
                stats['transfers'] = transfer_data

            return Response({
                'success': True,
                'data': stats
            }, status=status.HTTP_200_OK)

        except Exception:
            return server_error('تعذّر جلب الإحصائيات')

    @swagger_auto_schema(
        operation_description="Browse all active stores",
        manual_parameters=[
            openapi.Parameter(
                'search',
                openapi.IN_QUERY,
                description="Search in store name and description",
                type=openapi.TYPE_STRING,
                required=False
            )
        ],
        responses={200: UserStoreSerializer(many=True)}
    )
    def list(self, request, *args, **kwargs):
        """List all active stores (public endpoint)"""
        from django.db.models import Q

        queryset = self.get_queryset()

        # Search functionality
        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) |
                Q(description__icontains=search) |
                Q(user__username__icontains=search)
            )

        # المحافظة — fixed choice on Location.state, so an exact
        # (case-insensitive) match reliably finds stores in that governorate.
        governorate = request.query_params.get('governorate')
        if governorate:
            queryset = queryset.filter(default_location__state__iexact=governorate)

        total = queryset.order_by().count()
        stores, next_page = _page_of(request, queryset.order_by('-rating', '-id'), 50, 100)

        return Response({
            'success': True,
            'count': total,
            'next_page': next_page,
            'data': self.get_serializer(stores, many=True).data
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="البحث عن متاجر قريبة من إحداثيات معينة",
        manual_parameters=[
            openapi.Parameter('lat', openapi.IN_QUERY, description='خط العرض', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('lng', openapi.IN_QUERY, description='خط الطول', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('radius', openapi.IN_QUERY, description='نصف القطر بالكيلومتر (افتراضي: 10)', type=openapi.TYPE_NUMBER),
        ],
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def nearby(self, request):
        """GET /api/stores/nearby/?lat=33.3&lng=44.4&radius=10"""
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        radius = request.query_params.get('radius', 10)
        if not lat or not lng:
            return Response({
                'success': False,
                'message': 'lat و lng مطلوبان',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)
        try:
            queryset = UserStore.objects.filter(
                is_active=True, default_location__isnull=False, user__is_active=True
            ).select_related('default_location', 'user')
            results = filter_by_proximity(queryset, lat, lng, radius, 'default_location')
            data = UserStoreSerializer(results, many=True, context={'request': request}).data
            return Response({
                'success': True,
                'message': f'تم العثور على {len(data)} متجر قريب',
                'count': len(data),
                'data': data,
            })
        except (ValueError, TypeError):
            return Response({
                'success': False,
                'message': 'قيم lat/lng/radius غير صالحة',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)


class LocationViewSet(viewsets.ModelViewSet):

    """إدارة مواقع المستخدم - User location management"""
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, StableOrderingFilter]
    filterset_class = LocationFilter
    search_fields = ['name', 'city', 'country', 'address_line1']
    ordering_fields = ['created_at', 'is_primary']
    ordering = ['-is_primary', '-created_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Location.objects.none()
        return Location.objects.filter(user=self.request.user, is_active=True)

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return LocationCreateUpdateSerializer
        return LocationSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = LocationSerializer(queryset, many=True, context={'request': request})
        return Response({
            'success': True,
            'message': 'تم جلب المواقع بنجاح',
            'count': queryset.count(),
            'data': serializer.data,
        })

    def retrieve(self, request, *args, **kwargs):
        location = self.get_object()
        serializer = LocationSerializer(location, context={'request': request})
        return Response({
            'success': True,
            'message': 'تم جلب الموقع بنجاح',
            'data': serializer.data,
        })

    def create(self, request, *args, **kwargs):
        serializer = LocationCreateUpdateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors,
            }, status=status.HTTP_400_BAD_REQUEST)
        # The first location a user saves becomes their primary one unless
        # they said otherwise: "primary" is what every pickup/delivery
        # lookup falls back to, and a sole location that is not primary is
        # never what anyone meant.
        is_first = not Location.objects.filter(user=request.user, is_active=True).exists()
        location = serializer.save(user=request.user,
                                   **({'is_primary': True} if is_first else {}))
        return Response({
            'success': True,
            'message': 'تم إضافة الموقع بنجاح',
            'data': LocationSerializer(location, context={'request': request}).data,
        }, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        location = self.get_object()
        serializer = LocationCreateUpdateSerializer(
            location, data=request.data, partial=partial, context={'request': request}
        )
        if not serializer.is_valid():
            return Response({
                'success': False,
                'message': 'بيانات غير صالحة',
                'errors': serializer.errors,
            }, status=status.HTTP_400_BAD_REQUEST)
        location = serializer.save()
        return Response({
            'success': True,
            'message': 'تم تحديث الموقع بنجاح',
            'data': LocationSerializer(location, context={'request': request}).data,
        })

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        location = self.get_object()
        location.is_active = False
        location.save(update_fields=['is_active'])
        return Response({
            'success': True,
            'message': 'تم حذف الموقع بنجاح',
            'data': None,
        })

    @swagger_auto_schema(
        operation_description="القائمة الثابتة لمحافظات العراق الـ18 مع إحداثيات مركز كل محافظة "
                               "(تُستخدم لتعبئة قائمة اختيار المحافظة وتوسيط الخريطة عليها)",
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def governorates(self, request):
        """GET /api/locations/governorates/ — fixed list, no DB query needed"""
        return Response({
            'success': True,
            'data': IRAQ_GOVERNORATES,
        })

    @swagger_auto_schema(
        operation_description="تعيين موقع كموقع رئيسي للمستخدم",
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=True, methods=['post'])
    def set_primary(self, request, pk=None):
        location = self.get_object()
        Location.objects.filter(user=request.user, is_primary=True).exclude(id=location.id).update(is_primary=False)
        location.is_primary = True
        location.save(update_fields=['is_primary'])
        return Response({
            'success': True,
            'message': 'تم تعيين الموقع الرئيسي بنجاح',
            'data': LocationSerializer(location, context={'request': request}).data,
        })

    @swagger_auto_schema(
        operation_description="البحث عن منتجات قريبة من إحداثيات معينة",
        manual_parameters=[
            openapi.Parameter('lat', openapi.IN_QUERY, description='خط العرض', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('lng', openapi.IN_QUERY, description='خط الطول', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('radius', openapi.IN_QUERY, description='نصف القطر بالكيلومتر (افتراضي: 10)', type=openapi.TYPE_NUMBER),
        ],
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def nearby_items(self, request):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        radius = request.query_params.get('radius', 10)
        if not lat or not lng:
            return Response({
                'success': False,
                'message': 'lat و lng مطلوبان',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)
        try:
            queryset = Item.objects.filter(
                open_seller_q('owner'), status='active', current_location__isnull=False
            ).select_related('current_location', 'owner', 'category').prefetch_related('images')
            results = filter_by_proximity(queryset, lat, lng, radius, 'current_location')
            data = ItemSerializer(results, many=True, context={'request': request}).data
            return Response({
                'success': True,
                'message': f'تم العثور على {len(data)} منتج قريب',
                'count': len(data),
                'data': data,
            })
        except (ValueError, TypeError):
            return Response({
                'success': False,
                'message': 'قيم lat/lng/radius غير صالحة',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)

    @swagger_auto_schema(
        operation_description="البحث عن متاجر قريبة من إحداثيات معينة",
        manual_parameters=[
            openapi.Parameter('lat', openapi.IN_QUERY, description='خط العرض', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('lng', openapi.IN_QUERY, description='خط الطول', type=openapi.TYPE_NUMBER, required=True),
            openapi.Parameter('radius', openapi.IN_QUERY, description='نصف القطر بالكيلومتر (افتراضي: 10)', type=openapi.TYPE_NUMBER),
        ],
        responses={200: openapi.Schema(type=openapi.TYPE_OBJECT)}
    )
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def nearby_stores(self, request):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        radius = request.query_params.get('radius', 10)
        if not lat or not lng:
            return Response({
                'success': False,
                'message': 'lat و lng مطلوبان',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)
        try:
            queryset = UserStore.objects.filter(
                is_active=True, default_location__isnull=False, user__is_active=True
            ).select_related('default_location', 'user')
            results = filter_by_proximity(queryset, lat, lng, radius, 'default_location')
            data = UserStoreSerializer(results, many=True, context={'request': request}).data
            return Response({
                'success': True,
                'message': f'تم العثور على {len(data)} متجر قريب',
                'count': len(data),
                'data': data,
            })
        except (ValueError, TypeError):
            return Response({
                'success': False,
                'message': 'قيم lat/lng/radius غير صالحة',
                'data': None,
            }, status=status.HTTP_400_BAD_REQUEST)


class RatingViewSet(viewsets.ModelViewSet):
    """User ratings"""
    queryset = Rating.objects.all()
    serializer_class = RatingSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = RatingFilter

    def get_serializer_class(self):
        if self.action == 'create':
            return RatingCreateSerializer
        return RatingSerializer

    def perform_create(self, serializer):
        serializer.save(rater=self.request.user)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.rater != request.user:
            return Response({'detail': 'لا يمكنك تعديل تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.rater != request.user:
            return Response({'detail': 'لا يمكنك حذف تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)


class ProductReviewViewSet(viewsets.ModelViewSet):
    """تقييمات المنتجات - نجوم وتعليق وصورة اختيارية"""
    queryset = ProductReview.objects.select_related('reviewer', 'item').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_class = ProductReviewFilter
    ordering_fields = ['created_at', 'score']
    ordering = ['-created_at']
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductReviewCreateSerializer
        return ProductReviewSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return ProductReview.objects.none()
        return user_related(ProductReview.objects.select_related('item'), 'reviewer')

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.reviewer != request.user:
            return Response({'detail': 'لا يمكنك تعديل تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.reviewer != request.user:
            return Response({'detail': 'لا يمكنك حذف تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['get'], url_path='my-review/(?P<item_id>[^/.]+)')
    def my_review(self, request, item_id=None):
        """جلب تقييمي الخاص لمنتج معين"""
        review = (ProductReview.objects.filter(reviewer=request.user, item_id=item_id).first()
                  if str(item_id).isdigit() else None)
        if not review:
            return Response({'detail': 'لم تقيّم هذا المنتج بعد'}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProductReviewSerializer(review, context={'request': request}).data)


class StoreReviewViewSet(viewsets.ModelViewSet):
    """تقييمات المتاجر - نجوم وتعليق"""
    queryset = StoreReview.objects.select_related('reviewer', 'store').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_class = StoreReviewFilter
    ordering_fields = ['created_at', 'score']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return StoreReviewCreateSerializer
        return StoreReviewSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return StoreReview.objects.none()
        return StoreReview.objects.select_related('reviewer', 'store').all()

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.reviewer != request.user:
            return Response({'detail': 'لا يمكنك تعديل تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.reviewer != request.user:
            return Response({'detail': 'لا يمكنك حذف تقييم شخص آخر'}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['get'], url_path='my-review/(?P<store_id>[^/.]+)')
    def my_review(self, request, store_id=None):
        """جلب تقييمي الخاص لمتجر معين"""
        review = (StoreReview.objects.filter(reviewer=request.user, store_id=store_id).first() if str(store_id).isdigit() else None)
        if not review:
            return Response({'detail': 'لم تقيّم هذا المتجر بعد'}, status=status.HTTP_404_NOT_FOUND)
        return Response(StoreReviewSerializer(review, context={'request': request}).data)


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """View transactions"""
    queryset = Transaction.objects.all()
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, StableOrderingFilter]
    filterset_class = TransactionFilter
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        # Fix: Handle swagger schema generation
        if getattr(self, 'swagger_fake_view', False):
            return Transaction.objects.none()
        return user_related(item_related(Transaction.objects.all(), 'item'), 'user').filter(
            user=self.request.user)


class CategoryViewSet(viewsets.ModelViewSet):  # Changed from ReadOnlyModelViewSet
    """View and manage categories"""
    serializer_class = CategorySerializer
    # التصنيفات بيانات مرجعية صغيرة وثابتة تقريباً (~100 سجل) — التقسيم
    # لصفحات كان يقصّها عند PAGE_SIZE الافتراضي (20)، فيرجع الطرف المسطّح
    # (بلا ?parent=) 20 عنصراً فقط بدل الشجرة كاملة، ما كان يُخفي أغلب
    # التصنيفات الرئيسية والفرعية عن التطبيق.
    pagination_class = None

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsSuperAdmin]
        else:
            permission_classes = [AllowAny]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        qs = Category.objects.filter(is_active=True).order_by('order')
        # ?parent=null → root categories only; ?parent=<id> → children of that category
        parent_param = self.request.query_params.get('parent')
        if parent_param is not None:
            if parent_param.lower() == 'null':
                qs = qs.filter(parent__isnull=True)
            else:
                qs = qs.filter(parent_id=parent_param) if parent_param.isdigit() else qs.none()
        return qs

    def get_serializer_context(self):
        # يُحسب مرّة واحدة لكل طلب (لا لكل تصنيف) ليعرف الـ serializer أيّ
        # تصنيف (رئيسي أو فرعي) لديه فعلاً منتج واحد على الأقل منشور
        # (has_products) — يُستخدم في التطبيق لإخفاء التصنيفات الفارغة من
        # بنر الصفحة الرئيسية.
        context = super().get_serializer_context()
        context['active_category_ids'] = set(
            Item.objects.filter(status='active')
            .filter(Q(fixed_listing__isnull=False) | Q(auction_listing__isnull=False))
            .values_list('category_id', flat=True)
            .distinct()
        )
        children_map = {}
        for child_id, parent_id in Category.objects.filter(
            is_active=True, parent__isnull=False
        ).values_list('id', 'parent_id'):
            children_map.setdefault(parent_id, []).append(child_id)
        context['children_map'] = children_map
        return context

    def destroy(self, request, *args, **kwargs):
        # A hard delete here would CASCADE every child category, and raise
        # an unhandled ProtectedError (500) the moment any Item still
        # references it (Item.category is on_delete=PROTECT). Soft-deleting
        # matches the is_active flag every other list/detail view already
        # filters on, so the category just stops appearing anywhere.
        category = self.get_object()
        category.is_active = False
        category.save(update_fields=['is_active'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get all active items in a category"""
        category = self.get_object()
        items = Item.objects.filter(category=category, status='active').filter(
            Q(fixed_listing__isnull=False) | Q(auction_listing__isnull=False)
        )
        serializer = ItemListSerializer(items, many=True, context={'request': request})
        return Response(serializer.data)


import uuid
from .models import Wallet, DepositRequest, WithdrawalRequest, get_or_create_wallet
from .serializers import (
    WalletSerializer, DepositCreateSerializer, DepositRequestSerializer,
    WithdrawalCreateSerializer, WithdrawalRequestSerializer
)


class WalletViewSet(viewsets.ViewSet):
    """User wallet: view balance, deposit (simulated), withdraw (admin-approved)."""
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['get'])
    def me(self, request):
        """Get current user's wallet."""
        wallet = get_or_create_wallet(request.user)
        return Response({'success': True, 'data': WalletSerializer(wallet).data})

    @action(detail=False, methods=['get'])
    def activity(self, request):
        """Every movement of this user's money with a direction and a
        from → to pair, plus what is parked in escrow. Read-only; the manual
        re-sync of unfinished card top-ups is POST /payments/sync-pending/."""
        from .services.wallet_activity import build_activity
        try:
            limit = max(1, min(int(request.query_params.get('limit', 200)), 500))
        except (TypeError, ValueError):
            limit = 200
        return Response({'success': True, 'data': build_activity(request.user, limit=limit)})

    # The pre-gateway `deposit` action (a *simulated* card charge that credited
    # the wallet instantly) used to live here. It stayed reachable after the
    # real gateway shipped, which let any signed-in user mint balance with one
    # POST. Top-ups now go through /api/payments/ only — see payment_views.

    @swagger_auto_schema(request_body=WithdrawalCreateSerializer)
    @action(detail=False, methods=['post'])
    def withdraw(self, request):
        """Request a withdrawal to your Mastercard. Funds are HELD until admin approves."""
        serializer = WithdrawalCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'success': False, 'errors': serializer.errors},
                            status=status.HTTP_400_BAD_REQUEST)

        refusal = withdrawal_refusal(request.user)
        if refusal:
            return Response({'success': False, 'message': refusal}, status=status.HTTP_400_BAD_REQUEST)

        amount = serializer.validated_data['amount']
        account_number = serializer.validated_data['account_number']

        try:
            with transaction.atomic():
                wallet = Wallet.objects.select_for_update().get(
                    pk=get_or_create_wallet(request.user).pk
                )
                if wallet.available_balance < amount:
                    return Response({
                        'success': False,
                        'message': f'رصيدك لا يكفي. المتاح {wallet.available_balance} د.ع'
                    }, status=status.HTTP_400_BAD_REQUEST)

                waiting, why = not_withdrawable(request.user)
                if wallet.available_balance - waiting < amount:
                    free = max(wallet.available_balance - waiting, Decimal('0'))
                    return Response({
                        'success': False,
                        'message': f'المتاح للسحب الآن {free:,.0f} د.ع. لا يُسحب بعد: {why}'
                    }, status=status.HTTP_400_BAD_REQUEST)

                if WithdrawalRequest.objects.filter(
                        user=request.user, status='pending').count() >= settings.WITHDRAWALS_MAX_PENDING:
                    return Response({
                        'success': False,
                        'message': f'لديك {settings.WITHDRAWALS_MAX_PENDING} طلبات سحب قيد المراجعة — انتظر البتّ فيها أو ألغِ أحدها'
                    }, status=status.HTTP_400_BAD_REQUEST)

                wallet.hold(amount)  # lock the funds
                wd = WithdrawalRequest.objects.create(
                    user=request.user,
                    amount=amount,
                    card_last4=account_number[-4:],
                    account_number=account_number,
                    holder_name=serializer.validated_data['holder_name'].strip(),
                    status='pending',
                )
                tell_reviewers(wd)

            return Response({
                'success': True,
                'message': 'أُرسل طلب السحب وهو قيد المراجعة',
                'data': WithdrawalRequestSerializer(wd).data
            }, status=status.HTTP_201_CREATED)
        except Exception:
            return server_error('تعذّر إرسال طلب السحب')

    @action(detail=False, methods=['post'], url_path=r'withdrawals/(?P<withdrawal_id>\d+)/cancel')
    def cancel_withdrawal(self, request, withdrawal_id=None):
        """Take back a request nobody has decided yet."""
        wd = get_object_or_404(WithdrawalRequest, pk=withdrawal_id, user=request.user)
        try:
            wd = cancel_withdrawal(wd)
        except WithdrawalReviewError:
            return Response({'success': False, 'message': 'بُتّ في هذا الطلب ولا يُلغى'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'success': True, 'message': 'أُلغي الطلب وعاد المبلغ إلى رصيدك',
                         'data': WithdrawalRequestSerializer(wd).data})

    @action(detail=False, methods=['get'])
    def my_deposits(self, request):
        qs = DepositRequest.objects.filter(user=request.user).order_by('-created_at')
        return Response({'success': True, 'data': DepositRequestSerializer(qs, many=True).data})

    @action(detail=False, methods=['get'])
    def my_withdrawals(self, request):
        qs = WithdrawalRequest.objects.filter(user=request.user).order_by('-created_at')
        return Response({'success': True, 'data': WithdrawalRequestSerializer(qs, many=True).data})


class AdminWithdrawalViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin reviews and approves/rejects withdrawals."""
    queryset = WithdrawalRequest.objects.all().order_by('-created_at')
    serializer_class = WithdrawalRequestSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['status']

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve: settle held funds and (simulated) pay out to the card."""
        wd = self.get_object()
        try:
            approve_withdrawal(wd, reviewed_by=request.user, admin_notes=request.data.get('admin_notes', ''))
            return Response({'success': True, 'message': 'اعتُمد السحب',
                             'data': WithdrawalRequestSerializer(wd).data})
        except WithdrawalReviewError as e:
            return Response({'success': False, 'message': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّر الاعتماد')

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject: release the held funds back to spendable balance."""
        wd = self.get_object()
        try:
            reject_withdrawal(wd, reviewed_by=request.user, admin_notes=request.data.get('admin_notes', ''))
            return Response({'success': True, 'message': 'رُفض السحب وأُعيد المبلغ',
                             'data': WithdrawalRequestSerializer(wd).data})
        except WithdrawalReviewError as e:
            return Response({'success': False, 'message': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return server_error('تعذّر الرفض')


class NotificationViewSet(mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """
    Notifications per user — persisted in DB, delivered via Firebase.
    The app fetches these on login to show missed notifications.

    Nothing else references a Notification row, so unlike Item/Reservation/
    Chat/etc. there's no cascade risk — a real hard delete is safe here, and
    it was simply never wired up (the app had no way to clear one at all).
    """
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = PageNumberPagination

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Notification.objects.none()
        return Notification.objects.filter(user=self.request.user)


    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark a single notification as read"""
        notification = self.get_object()
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        return Response({'status': 'ok'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark all notifications as read"""
        self.get_queryset().filter(is_read=False).update(is_read=True)
        return Response({'status': 'ok'})

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Return count of unread notifications"""
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'unread_count': count})

    @action(detail=False, methods=['delete'], url_path='clear-all')
    def clear_all(self, request):
        """Delete every notification for the current user"""
        self.get_queryset().delete()
        return Response({'status': 'ok'})