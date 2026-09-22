

from django.urls import path, include

from rest_framework.routers import DefaultRouter


# Import your viewsets (assuming they're in views.py)
from .views import (
    UserViewSet,
    ItemViewSet,
    FixedPriceListingViewSet,
    AuctionViewSet,
    ReservationViewSet,
    RepresentativeViewSet,
    ItemTransferViewSet,
    ChatViewSet,
    MessageViewSet,
    RatingViewSet,
    DisputeViewSet,
    ReportViewSet,
    TransactionViewSet,
    CategoryViewSet,
    UserStoreViewSet, AdminWithdrawalViewSet, WalletViewSet,
    LocationViewSet,
    NotificationViewSet,
    ProductReviewViewSet,
    StoreReviewViewSet,
    LiveSessionViewSet,
    DeliveryOrderViewSet,
    ReelViewSet,
)
from .legal_api import LegalDocumentViewSet


# Router for API endpoints
router = DefaultRouter()
router.register(r'users', UserViewSet, basename='user')
router.register(r'items', ItemViewSet, basename='item')
router.register(r'listings/fixed', FixedPriceListingViewSet, basename='fixed-listing')
router.register(r'auctions', AuctionViewSet, basename='auction')
router.register(r'reservations', ReservationViewSet, basename='reservation')
router.register(r'representatives', RepresentativeViewSet, basename='representative')
router.register(r'transfers', ItemTransferViewSet, basename='transfer')
router.register(r'chats', ChatViewSet, basename='chat')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'ratings', RatingViewSet, basename='rating')
router.register(r'disputes', DisputeViewSet, basename='dispute')
router.register(r'reports', ReportViewSet, basename='report')
router.register(r'transactions', TransactionViewSet, basename='transaction')
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'stores', UserStoreViewSet, basename='store')
router.register(r'wallet', WalletViewSet, basename='wallet')
router.register(r'admin/withdrawals', AdminWithdrawalViewSet, basename='admin-withdrawals')
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'product-reviews', ProductReviewViewSet, basename='product-review')
router.register(r'store-reviews', StoreReviewViewSet, basename='store-review')
router.register(r'live-sessions', LiveSessionViewSet, basename='live-session')
router.register(r'reels', ReelViewSet, basename='reel')
router.register(r'delivery-orders', DeliveryOrderViewSet, basename='delivery-order')
router.register(r'legal/documents', LegalDocumentViewSet, basename='legal-document')

from . import payment_views

urlpatterns = [

    path('', include(router.urls)),

    # Payments. Not a ViewSet: the webhook and the browser-return endpoint are
    # unauthenticated and have nothing to do with a resource's CRUD surface,
    # and routing them by provider name keeps adding a gateway to a settings
    # entry rather than a new URL block.
    path('payments/methods/', payment_views.payment_methods, name='payment-methods'),
    path('payments/create/', payment_views.create_payment, name='payment-create'),
    path('payments/return/', payment_views.payment_return, name='payment-return'),
    path('payments/sync-pending/', payment_views.sync_pending_payments,
         name='payment-sync-pending'),
    path('payments/webhook/<str:provider_name>/',
         payment_views.PaymentWebhookView.as_view(), name='payment-webhook'),
    path('payments/<str:reference>/', payment_views.payment_detail,
         name='payment-detail'),

]