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

]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
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
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes

CELERY_BEAT_SCHEDULE = {
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
        'schedule': crontab(minute='*/1'),  # Every 5 minutes
    },
    'process-ended-auctions': {
        'task': 'core.tasks.process_ended_auctions',
        'schedule': crontab(minute='*/1'),  # Every 5 minutes
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
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

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
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
}

# JWT Settings
# NOTE: 2 hours is an interim value, down from 60 (!) — a leaked access token
# can no longer be replayed for 2.5 days. The Flutter app does not yet call
# the refresh endpoint on 401, so dropping this further (e.g. to the more
# typical 15-60 min) should happen together with wiring up that refresh flow,
# or users will start seeing "session expired" mid-session.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=2),
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
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:63342",  # Your IDE's built-in server
    "http://127.0.0.1:8000",
    "http://localhost:8000",
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

# Agora Configuration (live shopping video)
# Django only issues RTC join tokens here — video/audio itself is carried
# entirely by Agora and never passes through this server.
AGORA_APP_ID = os.environ.get('AGORA_APP_ID', '')
AGORA_APP_CERTIFICATE = os.environ.get('AGORA_APP_CERTIFICATE', '')
AGORA_TOKEN_TTL_SECONDS = int(os.environ.get('AGORA_TOKEN_TTL_SECONDS', '3600'))

# Live Shopping
LIVE_SESSION_MAX_DURATION_HOURS = int(os.environ.get('LIVE_SESSION_MAX_DURATION_HOURS', '6'))

# AWS S3 + BunnyCDN (Reels — short vertical video)
# Video/thumbnail bytes never pass through Django: the app PUTs directly to
# S3 via presigned URLs (core/aws_utils.py), and playback is served through
# a BunnyCDN Pull Zone whose origin is the S3 bucket. Django only makes
# control-plane boto3 calls (presign/head_object/copy_object/delete_object).
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME', '')
AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME', '')
BUNNYCDN_PULL_ZONE_BASE_URL = os.environ.get('BUNNYCDN_PULL_ZONE_BASE_URL', '')
REEL_PRESIGNED_URL_TTL_SECONDS = int(os.environ.get('REEL_PRESIGNED_URL_TTL_SECONDS', '900'))
REEL_MAX_VIDEO_SIZE_BYTES = int(os.environ.get('REEL_MAX_VIDEO_SIZE_BYTES', str(200 * 1024 * 1024)))  # 200 MB
REEL_PENDING_UPLOAD_CLEANUP_HOURS = int(os.environ.get('REEL_PENDING_UPLOAD_CLEANUP_HOURS', '2'))

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
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'noreply@reselltrust.com')

# SMS Configuration (Twilio)
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '')

# Application Settings
RESERVATION_EXPIRY_HOURS = int(os.environ.get('RESERVATION_EXPIRY_HOURS', '48'))
MINIMUM_DEPOSIT_PERCENTAGE = int(os.environ.get('MINIMUM_DEPOSIT_PERCENTAGE', '10'))
MAXIMUM_DEPOSIT_PERCENTAGE = int(os.environ.get('MAXIMUM_DEPOSIT_PERCENTAGE', '50'))

# Auction Settings
AUCTION_BID_EXTENSION_MINUTES = int(os.environ.get('AUCTION_BID_EXTENSION_MINUTES', '5'))
AUCTION_MINIMUM_DURATION_HOURS = int(os.environ.get('AUCTION_MINIMUM_DURATION_HOURS', '24'))

# Security Settings (Production)
if not DEBUG:
    SECURE_SSL_REDIRECT = True
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
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
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