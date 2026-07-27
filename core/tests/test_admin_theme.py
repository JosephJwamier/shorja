"""
Phase 1 admin-theme regression tests.

Config in UNFOLD/settings.py only proves the dict is well-formed — it
doesn't prove the rendered page actually uses it. These log a real admin
session in and read the HTML that comes back, so a broken sidebar link
(e.g. a model that gets unregistered later) or a silently-skipped RTL
context processor fails here instead of only being noticed by eye.
"""
import pytest
from django.conf import settings
from django.test import Client

from core.models import User

pytestmark = pytest.mark.django_db


def _superuser():
    return User.objects.create_superuser(username='owner', password='CorrectHorse123!', email='owner@example.com')


def test_admin_index_renders_unfold_theme_and_arabic_rtl():
    client = Client()
    client.force_login(_superuser())

    response = client.get(f'/{settings.ADMIN_URL_PATH}')
    assert response.status_code == 200

    html = response.content.decode()
    assert 'dir="rtl"' in html
    assert 'لوحة تحكم بلة' in html
    assert 'material-symbols-outlined' in html


def test_admin_sidebar_lists_configured_groups_and_working_links():
    client = Client()
    client.force_login(_superuser())

    response = client.get(f'/{settings.ADMIN_URL_PATH}')
    html = response.content.decode()

    for group_title in (
        'المستخدمون والمتاجر', 'المنتجات والتصنيفات', 'البيع والمزادات',
        'الحجوزات والمعاملات', 'الثقة والمراسلات', 'التسويق والعروض',
        'البث المباشر', 'التوصيل', 'المال والمحافظ', 'الأمان والوصول',
    ):
        assert group_title in html, f'sidebar group "{group_title}" missing from rendered page'

    for path in (
        'core/user/', 'core/userstore/', 'core/storefollow/', 'core/location/', 'core/representative/',
        'core/category/', 'core/item/', 'core/itemlocationhistory/',
        'core/fixedpricelisting/', 'core/auctionlisting/', 'core/itemtransfer/', 'core/storagefeepayment/',
        'core/reservation/', 'core/transaction/',
        'core/rating/', 'core/productreview/', 'core/storereview/', 'core/chat/', 'core/message/',
        'core/storecoupon/', 'core/couponcollection/',
        'core/livesession/', 'core/livesessionlike/', 'core/livesessioncomment/',
        'core/deliverycompany/', 'core/deliveryorder/',
        'core/wallet/', 'core/depositrequest/', 'core/withdrawalrequest/', 'core/notification/',
        'auth/group/', 'axes/accessattempt/', 'otp_totp/totpdevice/', 'auditlog/logentry/',
    ):
        assert path in html, f'sidebar link for {path} missing from rendered page'


def test_login_page_is_arabic_and_rtl_even_when_logged_out():
    client = Client()
    response = client.get('/account/login/')
    assert response.status_code == 200
    html = response.content.decode()
    assert 'dir="rtl"' in html


def test_registered_models_have_arabic_labels():
    from django.apps import apps

    labels = {
        'user': 'المستخدمون',
        'category': 'التصنيفات',
        'deliverycompany': 'شركات التوصيل',
        'deliveryorder': 'طلبات التوصيل',
        'storefollow': 'متابعات المتاجر',
        'storecoupon': 'كوبونات المتاجر',
        'couponcollection': 'الكوبونات المُجمَّعة',
        'livesession': 'جلسات البث المباشر',
        'livesessionlike': 'إعجابات البث المباشر',
        'livesessioncomment': 'تعليقات البث المباشر',
        'userstore': 'المتاجر',
        'location': 'المواقع والعناوين',
        'representative': 'المندوبون',
        'item': 'المنتجات',
        'itemlocationhistory': 'سجل تنقّل المنتجات',
        'fixedpricelisting': 'قوائم السعر الثابت',
        'auctionlisting': 'المزادات',
        'itemtransfer': 'اتفاقيات إعادة البيع',
        'storagefeepayment': 'رسوم التخزين',
        'reservation': 'الحجوزات',
        'transaction': 'المعاملات المالية',
        'rating': 'تقييمات المستخدمين',
        'productreview': 'مراجعات المنتجات',
        'storereview': 'مراجعات المتاجر',
        'chat': 'المحادثات',
        'message': 'الرسائل',
        'wallet': 'المحافظ',
        'notification': 'الإشعارات',
        'depositrequest': 'طلبات الإيداع',
        'withdrawalrequest': 'طلبات السحب',
    }
    for model_name, expected_plural in labels.items():
        model = apps.get_model('core', model_name)
        assert str(model._meta.verbose_name_plural) == expected_plural, model_name


def test_23_new_models_plus_original_10_are_all_registered():
    from django.apps import apps
    from django.contrib import admin

    registered = {m._meta.model_name for m in admin.site._registry if m._meta.app_label == 'core'}
    # dispute: deliberately deferred to Phase 3 (needs the guarded
    # resolution console, not plain CRUD). itemimage/livesessionitem/bid:
    # deliberately inline-only (ItemImageInline/LiveSessionItemInline/
    # BidInline on their parent's admin) rather than standalone-registered.
    deliberately_unregistered = {'dispute', 'itemimage', 'livesessionitem', 'bid'}
    all_core_models = {m._meta.model_name for m in apps.get_app_config('core').get_models()}
    unregistered = all_core_models - registered - deliberately_unregistered
    assert unregistered == set(), f'models still missing from the admin: {unregistered}'


def test_every_registered_model_changelist_page_loads():
    """Catches what the sidebar-link check can't: a typo'd field in
    list_display/raw_id_fields/inlines that only blows up once Django
    actually tries to render that specific changelist."""
    from django.apps import apps
    from django.urls import reverse

    client = Client()
    client.force_login(_superuser())

    core_models = {m._meta.model_name: m for m in apps.get_app_config('core').get_models()}
    skip = {'dispute', 'itemimage', 'livesessionitem', 'bid'}

    failures = []
    for model_name, model in core_models.items():
        if model_name in skip:
            continue
        url = reverse(f'admin:core_{model_name}_changelist')
        response = client.get(url)
        if response.status_code != 200:
            failures.append(f'{model_name} -> {response.status_code}')

    assert failures == [], f'admin changelist pages that failed to render: {failures}'
