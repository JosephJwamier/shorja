"""
Phase 4 — the admin index page itself renders the dashboard (as opposed to
the aggregation math, covered in test_analytics.py).
"""
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client

from core.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _superuser():
    return User.objects.create_superuser(username='dash_owner', password='x', email='d@example.com')


def test_dashboard_renders_kpi_cards_and_charts():
    client = Client()
    client.force_login(_superuser())

    response = client.get(f'/{settings.ADMIN_URL_PATH}')
    assert response.status_code == 200
    html = response.content.decode()

    assert 'نبض المنصة' in html
    for label in (
        'تسجيلات جديدة', 'مستخدمون نشطون', 'إجمالي المبيعات', 'نزاعات مفتوحة',
        'طلبات سحب معلّقة', 'بث مباشر الآن', 'عمولات المندوبين', 'متاجر نشطة',
        'المحافظات النشطة', 'محافظ بأرصدة غير طبيعية',
    ):
        assert label in html, f'missing dashboard section: {label}'

    assert 'id="registrations-chart"' in html
    assert 'id="sales-chart"' in html
    assert 'registrations-series-data' in html
    assert 'sales-series-data' in html


def test_dashboard_shows_all_18_governorates_with_zero_activity():
    client = Client()
    client.force_login(_superuser())

    response = client.get(f'/{settings.ADMIN_URL_PATH}')
    html = response.content.decode()
    for name in ('بغداد', 'البصرة', 'نينوى', 'دهوك', 'السليمانية'):
        assert name in html
