"""
Item/Reservation must never be hard-deleted through the API: both are
CASCADE-referenced by Reservation/Dispute/DeliveryOrder/ItemTransfer/etc.,
so a real DELETE would silently wipe financial/legal history. Item deletion
soft-deletes via archive_item(); Reservation deletion is disabled outright
(use the `cancel` action instead).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Item, Reservation, User

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item(owner, item_status='active'):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='good', quantity=1, status=item_status,
    )


def make_reservation(item, buyer, res_status='pending'):
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status=res_status, expires_at=timezone.now() + timedelta(hours=1),
    )


@pytest.fixture
def client():
    return APIClient()


def test_owner_deleting_item_archives_instead_of_hard_deleting(client):
    owner = make_user('del_owner1')
    item = make_item(owner)
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/items/{item.id}/')

    assert response.status_code == status.HTTP_200_OK
    item.refresh_from_db()  # would raise Item.DoesNotExist if hard-deleted
    assert item.status == 'archived'
    assert 'حذف بواسطة المالك' in item.admin_notes


def test_non_owner_cannot_delete_item(client):
    owner = make_user('del_owner2')
    other = make_user('del_other2')
    item = make_item(owner)
    client.force_authenticate(user=other)

    response = client.delete(f'/api/items/{item.id}/')

    assert response.status_code == status.HTTP_403_FORBIDDEN
    item.refresh_from_db()
    assert item.status == 'active'


def test_deleting_an_already_archived_item_is_idempotent(client):
    owner = make_user('del_owner3')
    item = make_item(owner, item_status='archived')
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/items/{item.id}/')

    assert response.status_code == status.HTTP_200_OK
    item.refresh_from_db()
    assert item.status == 'archived'


def test_reservation_cannot_be_deleted_through_the_api(client):
    owner = make_user('del_owner4')
    buyer = make_user('del_buyer4')
    item = make_item(owner)
    reservation = make_reservation(item, buyer)
    client.force_authenticate(user=buyer)

    response = client.delete(f'/api/reservations/{reservation.id}/')

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert Reservation.objects.filter(id=reservation.id).exists()
