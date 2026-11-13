"""The owner could charge a reseller again for an agreement already executed.

`ItemTransferViewSet` was a plain ModelViewSet and `ItemTransferSerializer`
left `status`, `quantity` and the storage-fee fields writable. After approving
a wholesale transfer, the owner alone could send

    PATCH /api/transfers/{id}/   {"status": "pending", "quantity": 5}

and then POST approve/ again: `activate()` only checks that the status is
'pending', so the reseller's wallet was charged for five more units they never
asked for, as often as their balance allowed (1000 -> 900 -> 400 in the audit).

The generic create/update endpoints are now closed (405) and the serializer is
output-only. The owner's and reseller's real paths — approve, reject, cancel,
complete — are pinned alongside.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, ItemTransfer, User, get_or_create_wallet,
)

pytestmark = pytest.mark.django_db

PRICE = Decimal('100')


@pytest.fixture
def owner():
    return User.objects.create_user(username='tr_owner', password='pass12345')


@pytest.fixture
def reseller():
    user = User.objects.create_user(username='tr_reseller', password='pass12345')
    get_or_create_wallet(user).credit(Decimal('1000'))
    return user


@pytest.fixture
def transfer(owner, reseller):
    category, _ = Category.objects.get_or_create(name='trcat', slug='trcat')
    item = Item.objects.create(
        owner=owner, title='t', description='d', category=category,
        condition='new', quantity=10, status='active',
    )
    FixedPriceListing.objects.create(item=item, price=PRICE, deposit_percentage=10)
    return ItemTransfer.objects.create(
        item=item, from_user=owner, to_user=reseller, quantity=1)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def balance(user):
    return get_or_create_wallet(user).balance


def test_owner_cannot_rewind_an_executed_agreement_and_charge_again(owner, reseller, transfer):
    owner_client = client_for(owner)
    assert owner_client.post(f'/api/transfers/{transfer.id}/approve/').status_code == 200
    assert balance(reseller) == Decimal('900')

    rewind = owner_client.patch(f'/api/transfers/{transfer.id}/',
                                {'status': 'pending', 'quantity': 5}, format='json')
    again = owner_client.post(f'/api/transfers/{transfer.id}/approve/')

    assert rewind.status_code == 405
    assert again.status_code == 400  # activate() refuses: not pending
    transfer.refresh_from_db()
    assert transfer.status == 'active'
    assert transfer.quantity == 1
    assert balance(reseller) == Decimal('900')


@pytest.mark.parametrize('method', ['patch', 'put'])
@pytest.mark.parametrize('who', ['owner', 'reseller'])
def test_neither_side_can_edit_a_pending_agreement(method, who, owner, reseller, transfer):
    user = owner if who == 'owner' else reseller
    response = getattr(client_for(user), method)(
        f'/api/transfers/{transfer.id}/',
        {'status': 'active', 'quantity': 9, 'storage_fee_type': 'flat_rate',
         'storage_fee_amount': '999', 'wholesale_price_per_unit': '0.01'},
        format='json')

    assert response.status_code == 405
    transfer.refresh_from_db()
    assert transfer.status == 'pending'
    assert transfer.quantity == 1
    assert transfer.storage_fee_type == 'none'
    assert balance(reseller) == Decimal('1000')


def test_generic_create_is_closed(owner, reseller, transfer):
    response = client_for(reseller).post('/api/transfers/', {
        'item': transfer.item_id, 'quantity': 1, 'status': 'active'}, format='json')

    assert response.status_code == 405
    assert ItemTransfer.objects.count() == 1


def test_reseller_cannot_approve_their_own_request(reseller, transfer):
    response = client_for(reseller).post(f'/api/transfers/{transfer.id}/approve/')

    assert response.status_code == 403
    assert balance(reseller) == Decimal('1000')


def test_owner_approve_still_charges_once(owner, reseller, transfer):
    response = client_for(owner).post(f'/api/transfers/{transfer.id}/approve/')

    assert response.status_code == 200
    transfer.refresh_from_db()
    assert transfer.status == 'active'
    assert transfer.resulting_item is not None
    assert balance(reseller) == Decimal('1000') - PRICE


def test_reseller_can_still_cancel_and_owner_can_still_reject(owner, reseller, transfer):
    second = ItemTransfer.objects.create(
        item=transfer.item, from_user=owner, to_user=reseller, quantity=1)

    cancelled = client_for(reseller).post(f'/api/transfers/{transfer.id}/cancel/')
    rejected = client_for(owner).post(f'/api/transfers/{second.id}/reject/',
                                      {'rejection_reason': 'no'}, format='json')

    assert cancelled.status_code == 200
    assert rejected.status_code == 200
    transfer.refresh_from_db(); second.refresh_from_db()
    assert transfer.status == 'cancelled'
    assert second.status == 'rejected'


def test_reseller_can_still_complete_an_active_agreement(owner, reseller, transfer):
    client_for(owner).post(f'/api/transfers/{transfer.id}/approve/')

    response = client_for(reseller).post(f'/api/transfers/{transfer.id}/complete/')

    assert response.status_code == 200
    transfer.refresh_from_db()
    assert transfer.status == 'completed'
