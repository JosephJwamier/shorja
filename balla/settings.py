"""
ReSell Trust Django Settings
"""

import os
from pathlib import Path
from datetime import timedelta
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """Populate os.environ from a simple KEY=value .env file. Every setting
    below reads via os.environ.get(...), but nothing previously loaded .env
    into the process environment — it only worked by coincidence where a
    hardcoded default happened to match .env (e.g. the DB_* defaults).
    Real env vars (already set by the OS/IDE) take precedence and are not
    overwritten."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(BASE_DIR / '.env')

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'  # or os.path.join(BASE_DIR, 'staticfiles')

_INSECURE_SECRET_KEY_PLACEHOLDER = 'your-secret-key-here-change-in-production'
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', _INSECURE_SECRET_KEY_PLACEHOLDER)

# Fails closed: an unset/misconfigured DEBUG env var now serves Django's
# production error pages (safe) instead of the full debug page (unsafe).
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
    if host.strip()
]

# Hard-stop a production deploy that still carries dev-only defaults —
# these previously shipped silently (placeholder SECRET_KEY signs every JWT;
# a wildcard host defeats Host-header validation entirely).
if not DEBUG:
    if SECRET_KEY == _INSECURE_SECRET_KEY_PLACEHOLDER:
        raise ImproperlyConfigured(
            'DJANGO_SECRET_KEY must be set to a real, unique secret when DEBUG=False.'
        )
    if '*' in ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            'ALLOWED_HOSTS must not contain "*" when DEBUG=False — set it to your real domain(s).'
        )


def _check_reels_storage_config():
    """Fail-closed check for the reel storage settings below (R2 endpoint,
    bucket, public base URL). Called once at import time when DEBUG=False,
    after those settings are defined — see the call site near
    REELS_PUBLIC_BASE_URL. Raise ImproperlyConfigured with a message that
    names the offending variable; return None when everything is sane.

    Settings-only — no network call at boot. An empty S3_ENDPOINT_URL is
    deliberately allowed: it means plain AWS S3, which is a valid (if no
    longer preferred) backend, not a half-migrated one."""
    from urllib.parse import urlparse

    if not AWS_STORAGE_BUCKET_NAME:
        raise ImproperlyConfigured(
            'AWS_STORAGE_BUCKET_NAME must be set when DEBUG=False — reels have nowhere to upload.'
        )
    if not REELS_PUBLIC_BASE_URL:
        raise ImproperlyConfigured(
            'REELS_PUBLIC_BASE_URL must be set when DEBUG=False — every reel would be served '
            'with an empty video_url. Connect a custom domain to the R2 bucket and put it here.'
        )
    public = urlparse(REELS_PUBLIC_BASE_URL)
    if public.scheme != 'https':
        # Android 9+ and iOS refuse cleartext media, so an http:// base
        # fails on the device, not in the server logs.
        raise ImproperlyConfigured(
            f'REELS_PUBLIC_BASE_URL must start with https:// (got {REELS_PUBLIC_BASE_URL!r}).'
        )
    if public.hostname and public.hostname.endswith('.r2.dev'):
        # The r2.dev subdomain is rate-limited and bypasses the cache —
        # fine for a manual check, not for serving video to users.
        raise ImproperlyConfigured(
            'REELS_PUBLIC_BASE_URL points at an r2.dev subdomain — connect a custom domain '
            'to the bucket (R2 > Settings > Custom Domains) and use that instead.'
        )
    if S3_ENDPOINT_URL and urlparse(S3_ENDPOINT_URL).scheme != 'https':
        raise ImproperlyConfigured(
            f'S3_ENDPOINT_URL must be an https:// URL (got {S3_ENDPOINT_URL!r}).'
        )
    return None

# Admin panel is served at a non-default path so it isn't the first thing a
# scanner tries. Must end with '/'. Override per environment via .env.
ADMIN_URL_PATH = os.environ.get('ADMIN_URL_PATH', 'admin/')

# Hard-enforce an OTP-verified session before granting admin access. Left
# off by default so this ships without locking out staff who haven't
# enrolled a device yet — flip to 'True' only after every staff account has
# set up a device at /account/two_factor/setup/. See core/admin_site.py.
ADMIN_REQUIRE_2FA = os.environ.get('ADMIN_REQUIRE_2FA', 'False') == 'True'

# Application definition
INSTALLED_APPS = [
    # Must be first: without 'daphne' registered, `manage.py runserver`
    # falls back to Django's plain WSGI command (no WebSocket support at
    # all) even though channels/ASGI_APPLICATION are configured below, and
    # running `daphne balla.asgi:application` directly skips static-file
    # serving entirely. Registering it here makes daphne's own runserver
    # override take effect, which wraps the ASGI app in
    # ASGIStaticFilesHandler in DEBUG — WebSockets and static files both
    # work through the one ordinary `manage.py runserver` command.
    'daphne',

    # Must precede django.contrib.admin: Django's app_directories template
    # loader takes the first match by INSTALLED_APPS order, and unfold
    # overrides admin/*.html to theme the panel.
    'unfold',
    'unfold.contrib.import_export',

    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'drf_yasg',  # API documentation
    'channels',  # WebSocket support for chat
    'django_celery_beat',  # Scheduled tasks
    'django_celery_results',  # Store task results in DB

    # Admin panel security & auditing (Phase 0)
    'django_otp',
    'django_otp.plugins.otp_static',
    'django_otp.plugins.otp_totp',
    'two_factor',
    'axes',
    'auditlog',

    # Admin reports & exports (Phase 7)
    'import_export',

    # Your apps
    'core',
    'ledger',

]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # مباشرة بعد SecurityMiddleware كما توثّق whitenoise. Caddy يخدم
    # /static/ في الإنتاج، لكن هذه تضمن أن لوحة الإدارة تعمل حتى لو شُغّل
    # الحاوية خلف وكيل آخر أو بلا وكيل أصلاً.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Forces Arabic/RTL for the admin + its login pages only — the API
    # itself stays on LANGUAGE_CODE (English) so client-facing strings
    # (e.g. DRF validation errors) don't change language under it.
    'core.middleware.AdminLocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_otp.middleware.OTPMiddleware',  # after auth: needs request.user
    'auditlog.middleware.AuditlogMiddleware',  # after auth: records request.user as actor
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',  # must stay last
]

# axes checks credentials via the auth backend chain, so its backend must
# run first — otherwise a locked-out account could still authenticate.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]


ROOT_URLCONF = 'balla.urls'  # NOT 'resell_trust.urls'
# settings.py
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Feeds LANGUAGE_BIDI into templates — what the admin theme
                # uses to switch dir="rtl" (see AdminLocaleMiddleware).
                'django.template.context_processors.i18n',
            ],
        },
    },
]

WSGI_APPLICATION = 'balla.wsgi.application'
ASGI_APPLICATION = 'balla.asgi.application'

# Database - MySQL Configuration
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME', 'balla'),
        'USER': os.environ.get('DB_USER', 'root'),
        'PASSWORD': os.environ.get('DB_PASSWORD', '1234'),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '3306'),
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            'charset': 'utf8mb4',
        },
    }
}

# Redis Cache & Celery
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')

# ── Wevlix (رسائل التحقّق عبر واتساب) ────────────────────────────────────
# يُستدعى من الخادم بمفتاح سرّي، لا من التطبيق — فكل رسالة تمرّ بنا
# ويمكن خنقها وتسجيلها. (Firebase Phone Auth كانت تُرسل من الجهاز.)
#
# فارغ = الميزة معطّلة: نقاط التوثيق تردّ 503 برسالة واضحة بدل أن تنهار.
WEVLIX_API_KEY = os.environ.get('WEVLIX_API_KEY', '')
WEVLIX_BASE_URL = os.environ.get('WEVLIX_BASE_URL', 'https://api.wevlix.com')

# مهلة صلاحية الرمز عندنا. Wevlix لها مهلتها أيضاً؛ هذه تحدّ عمر الربط
# بين الرقم و messageId في الذاكرة المؤقتة.
OTP_TTL_SECONDS = int(os.environ.get('OTP_TTL_SECONDS', '600'))

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': f'{REDIS_URL}/1',
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        }
    }
}

# Celery Configuration
CELERY_BROKER_URL = f'{REDIS_URL}/0'
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_RESULT_BACKEND = 'django-db'  # Store results in database
CELERY_CACHE_BACKEND = 'django-cache'  # Use Django cache
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
# Same zone as TIME_ZONE so the crontab hours in CELERY_BEAT_SCHEDULE
# ('daily at 1 AM', 'weekly Sunday 3 AM') mean Baghdad night, not UTC.
CELERY_TIMEZONE = 'Asia/Baghdad'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes

CELERY_BEAT_SCHEDULE = {
    # تذكير البائع بالتسليم — داخل نافذة الدوام وحدها.
    #
    # مرّتان في اليوم لا كل ساعة: التذكير عن مهلة يوم كامل، وتكراره
    # أكثر من ذلك إلحاحٌ لا فائدة فيه. والمهمّة نفسها ترفض العمل خارج
    # النافذة، فهذه الساعات والحارس يقولان الشيء نفسه مرّتين عن قصد —
    # تغيير النافذة في .env لا يجب أن يتطلّب تعديل جدولة.
    'remind-sellers-to-hand-over': {
        'task': 'core.tasks.remind_sellers_to_hand_over',
        'schedule': crontab(hour='10,17', minute=0),
    },

    # Reservation Management
    'process-expired-reservations': {
        'task': 'core.tasks.process_expired_reservations',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
    },
    'check-cancellation-eligibility': {
        'task': 'core.tasks.check_cancellation_eligibility',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
    },
    'release-payments-after-week': {
        'task': 'core.tasks.release_payments_after_week',
        'schedule': crontab(hour='*/6', minute=0),  # Every 6 hours
    },
    'send-pending-reservation-reminders': {
        'task': 'core.tasks.send_pending_reservation_reminders',
        'schedule': crontab(hour='*/4', minute=0),  # Every 4 hours
    },

    # Auction Management
    'activate-upcoming-auctions': {
        'task': 'core.tasks.activate_upcoming_auctions',
        'schedule': crontab(minute='*/1'),  # Every minute
    },
    'process-ended-auctions': {
        'task': 'core.tasks.process_ended_auctions',
        'schedule': crontab(minute='*/1'),  # Every minute
    },
    'auto-complete-unclaimed-auction-reservations': {
        'task': 'core.tasks.auto_complete_unclaimed_auction_reservations',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
    },

    # Dispute Management
    'auto-close-resolved-disputes': {
        'task': 'core.tasks.auto_close_resolved_disputes',
        'schedule': crontab(hour=1, minute=0),  # Daily at 1 AM
    },

    # User Trust & Reputation
    'calculate-trust-scores': {
        'task': 'core.tasks.calculate_user_trust_scores',
        'schedule': crontab(hour=2, minute=0),  # Daily at 2 AM
    },

    # Cleanup Tasks
    'cleanup-old-messages': {
        'task': 'core.tasks.cleanup_old_messages',
        'schedule': crontab(hour=3, minute=0, day_of_week=0),  # Weekly on Sunday at 3 AM
    },

    # Live Shopping
    'auto-end-stale-live-sessions': {
        'task': 'core.tasks.auto_end_stale_live_sessions',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
    },

    # Reels
    'cleanup-orphaned-reel-uploads': {
        'task': 'core.tasks.cleanup_orphaned_reel_uploads',
        'schedule': crontab(minute='*/30'),  # Every 30 minutes
    },
}

# Channels (WebSocket)
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',

        'CONFIG': {
            'hosts': [REDIS_URL],
        },
    },
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
# Storage stays UTC (USE_TZ=True); this only decides how times are
# rendered in the admin/templates and how Celery reads its crontabs.
# MySQL REQUIREMENT: any non-UTC zone makes Django wrap date lookups in
# CONVERT_TZ(), which needs MySQL's time zone tables loaded
# (`mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root -p mysql`).
# core/checks.py refuses `migrate` when they are missing.
TIME_ZONE = 'Asia/Baghdad'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# ضغط الملفات الثابتة وتسميتها ببصمة محتواها، فيُخزّنها المتصفح للأبد
# بأمان ويُبطلها أي تغيير تلقائياً.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# Project-level static dir is optional (the admin theme ships its own);
# listing a missing path is a permanent staticfiles.W004 on every command.
STATICFILES_DIRS = [d for d in [BASE_DIR / 'static'] if d.exists()]

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'core.User'

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # The browsable HTML API is a dev convenience only: it is the surface
    # behind DRF's CVE-2024-21520 (XSS) and it leaks endpoint shapes, so
    # production answers JSON and nothing else.
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ] + (['rest_framework.renderers.BrowsableAPIRenderer'] if DEBUG else []),
    # No global throttle: the app is IsAuthenticated by default and Iraqi
    # carriers CGNAT thousands of users behind one IP, so a blanket per-IP
    # limit would hit real users first. Only the auth endpoints are
    # throttled (core/throttling.py, core/auth_views.py) — per IP *and* per
    # target account, since the second is what actually stops credential
    # stuffing and reset-code guessing when the first must stay generous.
    'DEFAULT_THROTTLE_RATES': {
        'auth_login': '60/min',           # per IP: login/, token/, social-login/
        'auth_login_account': '5/min',    # per submitted username
        'auth_register': '30/hour',       # per IP
        'auth_reset': '30/hour',          # per IP: reset-password[-confirm]/
        'auth_reset_email': '5/hour',
        # رسائل التحقّق تكلّف مالاً حقيقياً لكل نداء ناجح، فالحدّ هنا
        # حماية من فاتورة لا من إساءة فقط. والتحقّق أرخص بكثير فيُترك
        # أوسع — وإلا حُبس من أخطأ الرمز مرّة.
        'otp_send': '10/hour',
        'otp_verify': '30/hour',     # per email — a 6-digit code needs this
    },
}

# JWT Settings
# 30 minutes: the app's ApiClient silently refreshes on 401 (one in-flight
# refresh, then the original request is replayed), so a short access token
# costs users nothing and bounds how long a leaked one is replayable.
# Refresh tokens rotate and the old one is blacklisted on every use.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
}


SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header',
            'description': 'JWT Authorization header using the Bearer scheme. Example: "Bearer {token}"'
        }
    },
    'USE_SESSION_AUTH': False,  # Disable session auth
    'JSON_EDITOR': True,
    'SUPPORTED_SUBMIT_METHODS': [
        'get',
        'post',
        'put',
        'delete',
        'patch'
    ],
}

# django-unfold: admin panel theme (Phase 1) + full sidebar (Phase 2).
# Sidebar groups link to specific model changelists, so every model
# registered in core/admin.py needs an entry here too — one not registered
# in core/admin.py yet stays reachable only by direct URL, not the sidebar.
from django.urls import reverse_lazy  # noqa: E402

UNFOLD = {
    'SITE_TITLE': 'بلة · الإدارة',
    'SITE_HEADER': 'لوحة تحكم بلة',
    'SITE_SUBHEADER': 'ReSell Trust',
    'SITE_SYMBOL': 'storefront',
    'SHOW_HISTORY': True,
    'SHOW_VIEW_ON_SITE': False,
    'BORDER_RADIUS': '6px',
    'DASHBOARD_CALLBACK': 'core.dashboard.dashboard_callback',
    'COLORS': {
        'primary': {
            '50': 'oklch(98% .012 200)',
            '100': 'oklch(95% .03 200)',
            '200': 'oklch(90% .055 200)',
            '300': 'oklch(82% .085 200)',
            '400': 'oklch(70% .105 200)',
            '500': 'oklch(58% .105 200)',
            '600': 'oklch(48% .095 200)',
            '700': 'oklch(40% .085 200)',
            '800': 'oklch(32% .065 200)',
            '900': 'oklch(24% .05 200)',
            '950': 'oklch(16% .035 200)',
        },
    },
    'SIDEBAR': {
        'show_search': True,
        'show_all_applications': False,
        'navigation': [
            {
                'title': 'النزاعات',
                'items': [
                    {'title': 'مركز حل النزاعات', 'icon': 'balance', 'link': reverse_lazy('admin:core_dispute_changelist')},
                ],
            },
            {
                'title': 'المستخدمون والمتاجر',
                'items': [
                    {'title': 'المستخدمون', 'icon': 'person', 'link': reverse_lazy('admin:core_user_changelist')},
                    {'title': 'المتاجر', 'icon': 'storefront', 'link': reverse_lazy('admin:core_userstore_changelist')},
                    {'title': 'متابعة المتاجر', 'icon': 'favorite', 'link': reverse_lazy('admin:core_storefollow_changelist')},
                    {'title': 'المواقع والعناوين', 'icon': 'location_on', 'link': reverse_lazy('admin:core_location_changelist')},
                    {'title': 'المندوبون', 'icon': 'badge', 'link': reverse_lazy('admin:core_representative_changelist')},
                ],
            },
            {
                'title': 'المنتجات والتصنيفات',
                'items': [
                    {'title': 'التصنيفات', 'icon': 'category', 'link': reverse_lazy('admin:core_category_changelist')},
                    {'title': 'المنتجات', 'icon': 'inventory_2', 'link': reverse_lazy('admin:core_item_changelist')},
                    {'title': 'سجل تنقّل المنتجات', 'icon': 'route', 'link': reverse_lazy('admin:core_itemlocationhistory_changelist')},
                ],
            },
            {
                'title': 'البيع والمزادات',
                'items': [
                    {'title': 'قوائم السعر الثابت', 'icon': 'sell', 'link': reverse_lazy('admin:core_fixedpricelisting_changelist')},
                    {'title': 'المزادات', 'icon': 'gavel', 'link': reverse_lazy('admin:core_auctionlisting_changelist')},
                    {'title': 'اتفاقيات إعادة البيع', 'icon': 'swap_horiz', 'link': reverse_lazy('admin:core_itemtransfer_changelist')},
                    {'title': 'رسوم التخزين', 'icon': 'warehouse', 'link': reverse_lazy('admin:core_storagefeepayment_changelist')},
                ],
            },
            {
                'title': 'الحجوزات والمعاملات',
                'items': [
                    {'title': 'الحجوزات', 'icon': 'shopping_cart', 'link': reverse_lazy('admin:core_reservation_changelist')},
                    {'title': 'المعاملات المالية', 'icon': 'receipt_long', 'link': reverse_lazy('admin:core_transaction_changelist')},
                ],
            },
            {
                'title': 'الثقة والمراسلات',
                'items': [
                    {'title': 'التقييمات', 'icon': 'star', 'link': reverse_lazy('admin:core_rating_changelist')},
                    {'title': 'مراجعات المنتجات', 'icon': 'rate_review', 'link': reverse_lazy('admin:core_productreview_changelist')},
                    {'title': 'مراجعات المتاجر', 'icon': 'reviews', 'link': reverse_lazy('admin:core_storereview_changelist')},
                    {'title': 'المحادثات', 'icon': 'forum', 'link': reverse_lazy('admin:core_chat_changelist')},
                    {'title': 'الرسائل', 'icon': 'chat', 'link': reverse_lazy('admin:core_message_changelist')},
                ],
            },
            {
                'title': 'التسويق والعروض',
                'items': [
                    {'title': 'كوبونات المتاجر', 'icon': 'confirmation_number', 'link': reverse_lazy('admin:core_storecoupon_changelist')},
                    {'title': 'الكوبونات المُجمَّعة', 'icon': 'redeem', 'link': reverse_lazy('admin:core_couponcollection_changelist')},
                ],
            },
            {
                'title': 'البث المباشر',
                'items': [
                    {'title': 'الجلسات الحية', 'icon': 'live_tv', 'link': reverse_lazy('admin:core_livesession_changelist')},
                    {'title': 'الإعجابات', 'icon': 'thumb_up', 'link': reverse_lazy('admin:core_livesessionlike_changelist')},
                    {'title': 'التعليقات', 'icon': 'chat_bubble', 'link': reverse_lazy('admin:core_livesessioncomment_changelist')},
                ],
            },
            {
                'title': 'الريلز',
                'items': [
                    {'title': 'الريلز', 'icon': 'movie', 'link': reverse_lazy('admin:core_reel_changelist')},
                    {'title': 'الإعجابات', 'icon': 'thumb_up', 'link': reverse_lazy('admin:core_reellike_changelist')},
                    {'title': 'التعليقات', 'icon': 'chat_bubble', 'link': reverse_lazy('admin:core_reelcomment_changelist')},
                ],
            },
            {
                'title': 'التوصيل',
                'items': [
                    {'title': 'شركات التوصيل', 'icon': 'local_shipping', 'link': reverse_lazy('admin:core_deliverycompany_changelist')},
                    {'title': 'طلبات التوصيل', 'icon': 'package_2', 'link': reverse_lazy('admin:core_deliveryorder_changelist')},
                    {'title': 'دفعات التوصيل المجمّعة', 'icon': 'merge_type', 'link': reverse_lazy('admin:core_deliverybatch_changelist')},
                    {'title': 'التسويات النقدية', 'icon': 'price_check', 'link': reverse_lazy('admin:core_cashsettlement_changelist')},
                ],
            },
            {
                'title': 'المال والمحافظ',
                'items': [
                    {'title': 'المحافظ', 'icon': 'account_balance_wallet', 'link': reverse_lazy('admin:core_wallet_changelist')},
                    {'title': 'طلبات الإيداع', 'icon': 'add_card', 'link': reverse_lazy('admin:core_depositrequest_changelist')},
                    {'title': 'طلبات السحب', 'icon': 'payments', 'link': reverse_lazy('admin:core_withdrawalrequest_changelist')},
                    {'title': 'الإشعارات', 'icon': 'notifications', 'link': reverse_lazy('admin:core_notification_changelist')},
                ],
            },
            {
                'title': 'الأمان والوصول',
                'items': [
                    {'title': 'مجموعات الصلاحيات', 'icon': 'shield_person', 'link': reverse_lazy('admin:auth_group_changelist')},
                    {'title': 'محاولات الدخول', 'icon': 'lock_clock', 'link': reverse_lazy('admin:axes_accessattempt_changelist')},
                    {'title': 'أجهزة التحقق بخطوتين', 'icon': 'verified_user', 'link': reverse_lazy('admin:otp_totp_totpdevice_changelist')},
                    {'title': 'سجل التدقيق', 'icon': 'history', 'link': reverse_lazy('admin:auditlog_logentry_changelist')},
                ],
            },
        ],
    },
}

# CORS Settings
# الأصول الإنتاجية تُضاف من البيئة (بوابة التوصيل، صفحات المشاركة) بدل
# تعديل الكود لكل نطاق جديد. المحلية تبقى للتطوير.
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:63342",  # Your IDE's built-in server
    "http://127.0.0.1:8000",
    "http://localhost:8000",
] + [o.strip() for o in os.environ.get('CORS_EXTRA_ORIGINS', '').split(',') if o.strip()]

# لوحة الإدارة ونماذج 2FA ترسل طلبات POST عبر HTTPS خلف وكيل؛ بدون هذه
# يرفض Django الطلب بـ CSRF verification failed.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Allow all origins in development only (Flutter Web uses random ports).
# Safe now that DEBUG defaults to False — this can only activate via an
# explicit DEBUG=True in the environment, never by omission.
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_CREDENTIALS = True

# Stripe Configuration
STRIPE_PUBLIC_KEY = os.environ.get('STRIPE_PUBLIC_KEY', '')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# =============================================================================
# Live media provider (live shopping video).
#
# Media never passes through this server: Django hands the app the
# credentials to publish or watch a stream, and answers the media server's
# "may this be published/played?" hooks. The server is our own SRS box —
# see infra/live/ for the stack and core/services/live_media/ for the
# provider. Every value below has to be set for live to work at all; the
# provider raises rather than hand out credentials it cannot honour.
# =============================================================================
LIVE_MEDIA_PROVIDER = os.environ.get('LIVE_MEDIA_PROVIDER', 'srs')
SRS_PUBLIC_BASE_URL = os.environ.get('SRS_PUBLIC_BASE_URL', '')      # https://live.example.com
SRS_APP = os.environ.get('SRS_APP', 'live')
SRS_HOOK_SECRET = os.environ.get('SRS_HOOK_SECRET', '')              # same value as infra/live/.env
# SRS's own http_server, where Django reads a stream's real playlist before
# signing the segment URIs in it (core/live_hooks.HlsPlaylistView). It must
# point at SRS directly: the public live domain routes .m3u8 back into that
# same view, which would recurse. On the shared host SRS runs on the host's
# network while Django is in a compose container, hence host.docker.internal
# there — the same way Caddy reaches it.
SRS_HLS_ORIGIN = os.environ.get('SRS_HLS_ORIGIN', 'http://127.0.0.1:8080')
LIVE_TOKEN_SECRET = os.environ.get('LIVE_TOKEN_SECRET', '')          # signs publish/play tokens
LIVE_TOKEN_TTL_SECONDS = int(os.environ.get('LIVE_TOKEN_TTL_SECONDS', '3600'))
TURN_HOST = os.environ.get('TURN_HOST', '')                          # live.example.com
# Plain STUN/TURN and TURN-over-TLS are separate listeners on separate
# ports, so one number cannot describe both: handing a client
# turns://host:3478 points TLS at a plaintext port and the relay simply
# never connects — silently, since ICE just reports no candidate.
TURN_PORT = int(os.environ.get('TURN_PORT', '3478'))
TURN_TLS_PORT = int(os.environ.get('TURN_TLS_PORT', '5349'))
TURN_SECRET = os.environ.get('TURN_SECRET', '')                      # same value as infra/live/.env

# Live Shopping
LIVE_SESSION_MAX_DURATION_HOURS = int(os.environ.get('LIVE_SESSION_MAX_DURATION_HOURS', '6'))

# Reels (short vertical video) — Cloudflare R2 object storage.
# Video/thumbnail bytes never pass through Django: the app PUTs directly to
# the bucket via presigned URLs (core/aws_utils.py), and playback is served
# from the bucket's custom domain on Cloudflare's CDN — zero egress fees,
# which is what made R2 win over S3 + BunnyCDN for an all-Iraq audience
# (BunnyCDN bills the Middle East at $0.06/GB, CloudFront at $0.11/GB).
# Django only makes control-plane boto3 calls (presign/head_object/
# copy_object/delete_object).
#
# R2 speaks the S3 API, so the credential names stay boto3's own. Leaving
# S3_ENDPOINT_URL empty falls back to plain AWS S3 (region-based endpoint).
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
# R2 ignores the region but boto3 insists on one; 'auto' is what Cloudflare documents.
AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME', 'auto')
AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME', '')
# https://<ACCOUNT_ID>.r2.cloudflarestorage.com — from R2 > Manage API Tokens.
S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT_URL', '')
# Public playback base, no trailing slash: the R2 bucket's custom domain
# (e.g. https://media.example.com). Never the r2.dev subdomain in
# production — it is rate-limited and bypasses the cache. Falls back to the
# old BunnyCDN variable so a not-yet-migrated .env keeps serving.
# بريد المنصّة إلى العملاء: مُطفأ حتى يُضبط مُرسِل حقيقي.
#
# كل بريد يخرج من المنصّة يمرّ بـNotificationService.send_email، وهذه الراية
# توقفه هناك: الإشعارات، والحجوزات، والمزادات، والمحفظة، وتفعيل البريد،
# واستعادة كلمة المرور بالبريد. الإشعارات الفورية (FCM) لا تتأثّر — وهي
# الناقل الفعلي للمعاملات، فالحجز يبلّغ صاحبه كما كان.
#
# مُطفأة افتراضياً لا مُشعلة: بلا SMTP كان send_mail يرمي استثناءً يُلتقط
# ويُسجّل كخطأ عند كل حجز ومزايدة، فتمتلئ السجلات بعطبٍ ليس عطباً. والإطفاء
# الصريح يقول ذلك بدل أن يبدو خللاً.
EMAIL_NOTIFICATIONS_ENABLED = (
    os.environ.get('EMAIL_NOTIFICATIONS_ENABLED', 'false').strip().lower()
    in ('1', 'true', 'yes', 'on')
)

# ---------------------------------------------------------------------------
# تذكير البائع بتسليم الطلب لشركة التوصيل
# ---------------------------------------------------------------------------
# بعد كم ساعة من **دفع المشتري** (لا من الحجز): مهلة البائع تبدأ حين تصير
# عليه مسؤولية، لا حين ضغط المشتري «احجز».
SELLER_HANDOVER_REMINDER_HOURS = int(
    os.environ.get('SELLER_HANDOVER_REMINDER_HOURS', '24'))

# نافذة الإرسال بتوقيت بغداد. رسالة واتساب الثالثة فجراً توقظ التاجر،
# ومن أيقظناه مرّة يُسكت الرقم — فنخسر معه إشعارات الحجز والمزاد.
SELLER_REMINDER_WINDOW_START = int(
    os.environ.get('SELLER_REMINDER_WINDOW_START', '9'))
SELLER_REMINDER_WINDOW_END = int(
    os.environ.get('SELLER_REMINDER_WINDOW_END', '21'))

# اسم قالب واتساب المعتمد عند Meta. فارغاً يُرسَل الإشعار الفوري وحده —
# الميزة تعمل منقوصةً لا أن تتعطّل.
WEVLIX_TEMPLATE_HANDOVER_REMINDER = os.environ.get(
    'WEVLIX_TEMPLATE_HANDOVER_REMINDER', '')

# رابط التطبيق على Google Play. فارغ قبل النشر: صفحات المشاركة تُظهر
# «قادم قريباً» بدل زرّ يقود إلى 404. يُضبط في .env يوم النشر بلا تعديل كود.
PLAY_STORE_URL = os.environ.get('PLAY_STORE_URL', '')

REELS_PUBLIC_BASE_URL = (
    os.environ.get('REELS_PUBLIC_BASE_URL')
    or os.environ.get('BUNNYCDN_PULL_ZONE_BASE_URL', '')
)
REEL_PRESIGNED_URL_TTL_SECONDS = int(os.environ.get('REEL_PRESIGNED_URL_TTL_SECONDS', '900'))
REEL_MAX_VIDEO_SIZE_BYTES = int(os.environ.get('REEL_MAX_VIDEO_SIZE_BYTES', str(200 * 1024 * 1024)))  # 200 MB
REEL_PENDING_UPLOAD_CLEANUP_HOURS = int(os.environ.get('REEL_PENDING_UPLOAD_CLEANUP_HOURS', '2'))

if not DEBUG:
    _check_reels_storage_config()

# Firebase Configuration
# Download your service account key from:
# Firebase Console -> Project Settings -> Service Accounts -> Generate new private key
FIREBASE_CREDENTIALS_PATH = os.environ.get(
    'FIREBASE_CREDENTIALS_PATH',
    BASE_DIR / 'firebase-credentials.json'
)

# Firestore-based chat sync is paused (Firebase billing disabled) — live chat uses
# the WebSocket transport instead. Flip to 'True' to reactivate Firestore chat sync.
FIRESTORE_CHAT_SYNC_ENABLED = os.environ.get('FIRESTORE_CHAT_SYNC_ENABLED', 'False') == 'True'

# Email Configuration
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
# reselltrust.com اسم قديم للمشروع ولا يملكه أحد منّا — رسالة تخرج
# منه تُعامَل كمنتحِلة وتسقط في البريد المزعج.
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@shorjeh.com')

# Application Settings
RESERVATION_EXPIRY_HOURS = int(os.environ.get('RESERVATION_EXPIRY_HOURS', '48'))
MINIMUM_DEPOSIT_PERCENTAGE = int(os.environ.get('MINIMUM_DEPOSIT_PERCENTAGE', '10'))
MAXIMUM_DEPOSIT_PERCENTAGE = int(os.environ.get('MAXIMUM_DEPOSIT_PERCENTAGE', '50'))

# Auction Settings
AUCTION_BID_EXTENSION_MINUTES = int(os.environ.get('AUCTION_BID_EXTENSION_MINUTES', '5'))
AUCTION_MINIMUM_DURATION_HOURS = int(os.environ.get('AUCTION_MINIMUM_DURATION_HOURS', '24'))

# Security Settings (Production)
if not DEBUG:
    # إلزامي خلف وكيل عكسي (Caddy/nginx): الوكيل ينهي TLS ويمرّر الطلب
    # إلى daphne عبر HTTP، فيظنّه Django غير آمن ويعيد توجيهه إلى HTTPS
    # إلى ما لا نهاية. هذه الترويسة هي ما يكسر الحلقة.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    # خطّافات SRS تُستثنى من إعادة التوجيه إلى HTTPS.
    #
    # SRS ينادي Django على الحلقة المحلية بـHTTP مباشرةً، لا عبر Caddy —
    # وهذا مقصود (انظر تعليق ports في deploy/docker-compose.yml): المرور
    # عبر api.shorjeh.com والعودة كان سيضع رحلة عامة كاملة في مسار كل
    # مشاهد. لكنه يعني أن الترويسة التي تكسر حلقة التوجيه أعلاه غائبة،
    # فيردّ Django 301 — وSRS يقرأ أي جواب غير {"code": 0} رفضاً، فيمنع
    # **كل** نشر و**كل** مشاهدة. ولا يظهر ذلك في أي سجل إلا سجل SRS.
    #
    # ولا يُضعِف هذا شيئاً: المسار غير منشور للعالم (daphne على
    # 127.0.0.1:8000 وحدها)، وحجّته سرٌّ مشترك لا هيئة النقل.
    SECURE_REDIRECT_EXEMPT = [r'^api/live/srs/']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# django-axes: lock out repeated failed admin-login attempts instead of
# allowing unlimited password guessing against staff accounts.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=30)
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_RESET_ON_SUCCESS = True

# django-two-factor-auth: route the admin's login redirect through the
# 2FA-aware login wizard instead of Django's plain admin login page. Staff
# who haven't enrolled a device still get an ordinary username/password
# login — the wizard only adds a token step once a confirmed device exists
# — so installing this does not lock anyone out by itself. The actual hard
# requirement is a separate opt-in: ADMIN_REQUIRE_2FA above.
LOGIN_URL = 'two_factor:login'
LOGIN_REDIRECT_URL = f'/{ADMIN_URL_PATH}'
TWO_FACTOR_PATCH_ADMIN = True

# django-auditlog: which sensitive models get a "who changed what, when"
# trail. Registered in core/apps.py so it applies regardless of whether the
# model is exposed in core/admin.py yet.
AUDITLOG_INCLUDE_ALL_MODELS = False

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            # 10 MB x 5 — a plain FileHandler had grown past 100k lines in
            # dev alone; in production it would eventually fill the disk.
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'maxBytes': 10 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'celery': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# =============================================================================
# Error monitoring (Sentry). Silent no-op until SENTRY_DSN is set, so dev
# and tests never ship events. Django + Celery integrations cover both the
# request path and the beat/worker tasks (auctions, payouts, cleanups),
# which is where a silent failure would otherwise cost real money.
# =============================================================================
SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
def _scrub_card_numbers(event, hint):
    """يُمرَّر إلى Sentry كـ before_send — انظر
    core/services/installments/cards.py. يُستورَد هنا بتأخير لأن settings
    تُقرأ قبل تحميل التطبيقات."""
    from core.services.installments.cards import scrub_sentry_event
    return scrub_sentry_event(event, hint)


if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        environment=os.environ.get('SENTRY_ENVIRONMENT', 'development' if DEBUG else 'production'),
        release=os.environ.get('SENTRY_RELEASE', ''),
        # Performance tracing is opt-in — errors are the point at launch.
        traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0')),
        # Never attach usernames/emails/IPs to events by default.
        send_default_pii=False,
        # ولا أرقام بطاقات: جسم الطلب يُرفَق مع الخطأ، ونداء فحص التقسيط
        # يحمل رقم بطاقة كي كارد — فأي 500 بعده كان سيشحن الرقم إلى متتبّع
        # الأخطاء، وهو آخر مكان يفكّر أحد بالبحث فيه عنه.
        before_send=_scrub_card_numbers,
    )

# =============================================================================
# بوابات الدفع
# -----------------------------------------------------------------------------
# كل مزوّد مدخل واحد هنا؛ إضافة بوابة جديدة = صنف في core/services/payments/
# وسطر في هذا القاموس. المزوّد بلا بيانات اعتماد لا يظهر للمستخدم أصلاً
# (enabled_providers)، فلا تُعرض طريقة دفع تفشل عند الضغط عليها.
#
# الأسرار من البيئة فقط. القيم الافتراضية هنا هي بيانات الاختبار المنشورة
# علناً في وثائق القاصة، وتصلح للـ sandbox لا غير.
# =============================================================================
PAYMENT_PROVIDERS = {
    'alqaseh': {
        'BASE_URL': os.environ.get('ALQASEH_BASE_URL', 'https://api-test.alqaseh.com/v1'),
        'CHECKOUT_BASE_URL': os.environ.get('ALQASEH_CHECKOUT_BASE_URL',
                                            'https://pay-test.alqaseh.com'),
        'CLIENT_ID': os.environ.get('ALQASEH_CLIENT_ID', ''),
        'CLIENT_SECRET': os.environ.get('ALQASEH_CLIENT_SECRET', ''),
        'TIMEOUT': int(os.environ.get('ALQASEH_TIMEOUT', '30')),
        'TOKEN_EXPIRY_HOURS': int(os.environ.get('ALQASEH_TOKEN_EXPIRY_HOURS', '2')),
    },
}

# مخطّط الرابط العميق الذي يعيد المتصفّح إلى التطبيق بعد الدفع.
APP_DEEP_LINK_SCHEME = os.environ.get('APP_DEEP_LINK_SCHEME', 'balla')

# =============================================================================
# التقسيط — مموّلون خارجيون (كي كارد)
# -----------------------------------------------------------------------------
# مموّل لا بوابة: هو من يدفع ثمن الطلب للمنصة، ثم يستقطع من راتب المشتري
# عشرة أشهر. النموذجان المتّفَق عليهما مع كي كارد يختلفان بحقل واحد:
#
#   upfront  (الافتراضي) تدفع كي كارد كامل المبلغ للمنصة الآن ناقص نسبة
#            التاجر، وتتابع هي الاستقطاع. لا مخاطرة ائتمانية ولا تمويل علينا.
#   monthly  تستقطع شهرياً وتودع في حساب المنصة، وتأخذ نسبة المشتري فقط —
#            لكن المنصة هي من يموّل البائع حتى آخر قسط. الحساب الذي رجّح
#            upfront في QICARD_INSTALLMENTS_README.md.
#
# النسب هنا هي ما عُرض علينا (10% من المشتري تُضاف على سعره، 5% من التاجر
# تُخصم من التسوية)؛ أي إعادة تفاوض تغيير متغيّر بيئة لا تغيير كود.
#
# المزوّد بلا بيانات اعتماد لا يظهر للمشتري أصلاً، و SANDBOX يجيب محلياً
# بلا شبكة (ولا يعمل إلا مع DEBUG) حتى تصل بيانات كي كارد الحقيقية.
# =============================================================================
INSTALLMENT_PROVIDERS = {
    'qicard': {
        'BASE_URL': os.environ.get('QICARD_BASE_URL', ''),
        'MERCHANT_ID': os.environ.get('QICARD_MERCHANT_ID', ''),
        'CLIENT_ID': os.environ.get('QICARD_CLIENT_ID', ''),
        'CLIENT_SECRET': os.environ.get('QICARD_CLIENT_SECRET', ''),
        'API_KEY': os.environ.get('QICARD_API_KEY', ''),
        'WEBHOOK_SECRET': os.environ.get('QICARD_WEBHOOK_SECRET', ''),
        'TIMEOUT': int(os.environ.get('QICARD_TIMEOUT', '30')),
        'MONTHS': int(os.environ.get('QICARD_MONTHS', '10')),
        'CUSTOMER_FEE_PERCENT': os.environ.get('QICARD_CUSTOMER_FEE_PERCENT', '10'),
        'MERCHANT_FEE_PERCENT': os.environ.get('QICARD_MERCHANT_FEE_PERCENT', '5'),
        'SETTLEMENT_MODE': os.environ.get('QICARD_SETTLEMENT_MODE', 'upfront'),
        # حدّان لا تجميل: خطة عشرة أشهر على مبلغ صغير كلّها رسوم بلا فائدة
        # لأحد، وسقفٌ يمنع خطأً مطبعياً من تمويل طلب بمليارات.
        'MIN_AMOUNT': os.environ.get('QICARD_MIN_AMOUNT', '250000'),
        'MAX_AMOUNT': os.environ.get('QICARD_MAX_AMOUNT', '5000000'),
        'CARD_LENGTHS': os.environ.get('QICARD_CARD_LENGTHS', '16'),
        'REQUIRE_LUHN': os.environ.get('QICARD_REQUIRE_LUHN', '1'),
        'SANDBOX': os.environ.get('QICARD_SANDBOX', '0'),
    },
}

# كم من نسبة المموّل من التاجر يحملها البائع فعلاً: 1 = كلّها (الافتراضي)،
# و0.5 مثلاً = المنصة تمتصّ نصفها من عمولتها لتشجيع التجار على التقسيط في
# فترة الإطلاق. تُجمَّد على الخطة لحظة التأكيد، فتغييرها لا يمسّ خطّة جارية.
INSTALLMENT_SELLER_FEE_SHARE = os.environ.get('INSTALLMENT_SELLER_FEE_SHARE', '1')

# =============================================================================
# Platform commission — what الشورجة keeps from each sale, taken from the
# SELLER when escrow is released (never added on top of what the buyer pays).
#
# One rate covers every sale channel: fixed price, live shopping and
# auctions alike. The auction path freezes it on the winner's reservation in
# auction_utils.create_winner_reservation, using the winning bid as the base.
# There is deliberately no auction-specific rate — auctions and fixed-price
# listings share a category, so per-category overrides
# (Category.commission_percent, inherited up the tree) are the only knob.
#
# This is the fallback for categories that set no rate of their own.
# See core/services/commission.py for the full rule set.
# =============================================================================
PLATFORM_COMMISSION_PERCENT = os.environ.get('PLATFORM_COMMISSION_PERCENT', '3')
PLATFORM_ACCOUNT_USERNAME = os.environ.get('PLATFORM_ACCOUNT_USERNAME', 'platform')

# العنوان العام الذي تصل إليه البوابة (بلا شرطة أخيرة).
#
# ضروري لأن الـ webhook يأتي من خوادم البوابة لا من التطبيق: العنوان الذي
# يستعمله التطبيق في التطوير هو 10.0.2.2:8000 وهو اسم داخلي للمحاكي، ولو
# أرسلناه للبوابة لأطلقت النداء في العدم. في التطوير يوضع هنا رابط النفق
# (ngrok/cloudflared)، وفي الإنتاج نطاق الـ API الحقيقي.
#
# إن تُرك فارغاً يُشتقّ العنوان من الطلب — يصحّ فقط لخادم معنوَن عاماً أصلاً.
PAYMENT_PUBLIC_BASE_URL = os.environ.get('PAYMENT_PUBLIC_BASE_URL', '')
