"""
Phase 8 — query performance. list_select_related on the FK columns shown in
list_display should keep each admin changelist's query count flat as row
count grows; without it, Django issues one extra query per row per FK
column (the classic N+1). These tests prove the *shape* of that: query
count with 3 rows should equal query count with 15 rows, not scale with N.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Category, Dispute, Item, Reservation, Transaction, User

pytestmark = pytest.mark.django_db


def _superuser():
    return User.objects.create_superuser(username='perf_owner', password='x', email='p@example.com')


def _query_count_for(client, url):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
        assert response.status_code == 200
    return len(ctx.captured_queries)


def make_reservations(n, owner, buyer):
    category, _ = Category.objects.get_or_create(name='perf-cat', slug='perf-cat')
    for i in range(n):
        item = Item.objects.create(owner=owner, title=f'item{i}', description='d', category=category,
                                    condition='good', quantity=1, status='active')
        Reservation.objects.create(
            item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('1'), total_price=Decimal('10'),
            status='confirmed', expires_at=timezone.now() + timedelta(days=1),
            completed_at=timezone.now(), is_deposit_only=False,
        )


def test_reservation_changelist_query_count_does_not_scale_with_row_count():
    owner, buyer = User.objects.create_user(username='perf_owner_r', password='x'), \
        User.objects.create_user(username='perf_buyer_r', password='x')
    client = Client()
    client.force_login(_superuser())
    url = reverse('admin:core_reservation_changelist')

    make_reservations(3, owner, buyer)
    small = _query_count_for(client, url)

    make_reservations(12, owner, buyer)  # now 15 total
    large = _query_count_for(client, url)

    assert large == small, f'query count grew with row count (N+1): {small} -> {large}'


def test_transaction_changelist_query_count_does_not_scale_with_row_count():
    user = User.objects.create_user(username='perf_txn_user', password='x')
    client = Client()
    client.force_login(_superuser())
    url = reverse('admin:core_transaction_changelist')

    for _ in range(3):
        Transaction.objects.create(user=user, transaction_type='payout', amount=Decimal('5'), status='completed')
    small = _query_count_for(client, url)

    for _ in range(12):
        Transaction.objects.create(user=user, transaction_type='payout', amount=Decimal('5'), status='completed')
    large = _query_count_for(client, url)

    assert large == small, f'query count grew with row count (N+1): {small} -> {large}'


def test_dispute_changelist_query_count_does_not_scale_with_row_count():
    owner, buyer = User.objects.create_user(username='perf_disp_owner', password='x'), \
        User.objects.create_user(username='perf_disp_buyer', password='x')
    category, _ = Category.objects.get_or_create(name='perf-cat2', slug='perf-cat2')
    client = Client()
    client.force_login(_superuser())
    url = reverse('admin:core_dispute_changelist')

    def make_disputes(n):
        for i in range(n):
            item = Item.objects.create(owner=owner, title=f'd-item{i}', description='d', category=category,
                                        condition='good', quantity=1, status='active')
            Dispute.objects.create(item=item, complainant=buyer, defendant=owner, title=f't{i}', description='d')

    make_disputes(3)
    small = _query_count_for(client, url)

    make_disputes(12)
    large = _query_count_for(client, url)

    assert large == small, f'query count grew with row count (N+1): {small} -> {large}'
