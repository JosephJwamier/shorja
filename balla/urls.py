"""
ReSell Trust - URL Configuration
Main urls.py file for single-app structure
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView
)
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework import permissions
from two_factor.urls import urlpatterns as tf_urls
from core.legal_views import legal_document_page
from core import share_views
from core.throttling import LoginUsernameThrottle
from rest_framework.throttling import ScopedRateThrottle


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """/api/auth/token/ is a second login door (username+password -> JWT);
    it gets the same per-IP + per-username budget as /api/auth/login/."""
    throttle_classes = [ScopedRateThrottle, LoginUsernameThrottle]
    throttle_scope = 'auth_login'

schema_view = get_schema_view(
    openapi.Info(
        title="ReSell Trust API",
        default_version='v1',
        description="""
        API documentation for ReSell Trust platform.

        ## Authentication
        Use the **Authorize** button to add your JWT token.
        Format: Bearer <your_token>

        ## Getting a Token
        1. Register or login via `/api/auth/login/`
        2. Copy the access token from the response
        3. Click **Authorize** and enter: Bearer <token>
        """,
        terms_of_service="https://www.reselltrust.com/terms/",
        contact=openapi.Contact(email="contact@reselltrust.com"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    # Two-factor login/enrollment wizard (also used as the admin's LOGIN_URL —
    # see settings.LOGIN_URL). tf_urls already bakes in its own 'account/'
    # prefix on every path, so it's included at the root here, not nested
    # under an extra one. Provides /account/login/,
    # /account/two_factor/setup/, /account/two_factor/qrcode/, etc.
    path('', include(tf_urls)),

    # Admin — path is env-configurable so it isn't sitting at the
    # well-known /admin/ every scanner tries first (see ADMIN_URL_PATH).
    path(settings.ADMIN_URL_PATH, admin.site.urls),

    # Authentication - JWT
    path('api/auth/token/', ThrottledTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/auth/token/verify/', TokenVerifyView.as_view(), name='token_verify'),

    # Public legal pages — علنية (رابط لمتاجر التطبيقات والإعلانات المدفوعة)
    # صفحات المشاركة على shorjeh.com. المسارات قصيرة عمداً: الرابط يُلصق
    # في واتساب ويُقرأ بالعين، و/p/123 أقصر من /share/product/123.
    #
    # تُخدم من نفس تطبيق Django لأن Caddy يوجّه النطاقين إلى web:8000؛
    # وهي عامة بلا مصادقة، فجالب واتساب لا يملك حساباً ولا يستطيع تسجيل
    # الدخول ليرسم بطاقة.
    # جذر shorjeh.com. يأتي بعد tf_urls في الترتيب، وهي لا تعرّف مساراً
    # فارغاً — فلولا ذلك لحجبته.
    path('', share_views.home, name='share-home'),
    path('p/<int:listing_id>', share_views.product, name='share-product'),
    path('s/<int:store_id>', share_views.store, name='share-store'),
    path('a/<int:auction_id>', share_views.auction, name='share-auction'),
    path('l/<int:session_id>', share_views.live, name='share-live'),

    path('legal/privacy/', legal_document_page, {'doc_type': 'privacy'}, name='legal-privacy'),
    path('legal/terms/', legal_document_page, {'doc_type': 'terms'}, name='legal-terms'),

    # API Routes
    path('api/', include('core.urls')),
    path('api/ledger/', include('ledger.urls')),

    # Additional custom endpoints
    path('api/auth/', include('core.auth_urls')),  # Custom auth endpoints
    path('api/webhooks/', include('core.webhook_urls')),  # Stripe webhooks
]

# Development only: media/static served by Django, and the public API
# explorer. In production the reverse proxy serves files, and the schema
# (a complete map of every endpoint, wallet and admin routes included) is
# not published to anonymous callers at all.
if settings.DEBUG:
    urlpatterns += [
        path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
        path('swagger.json', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    ]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
