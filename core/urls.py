

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

urlpatterns = [

    path('', include(router.urls)),

]