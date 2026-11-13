"""A representative could promote themselves through the generic endpoint.

`RepresentativeViewSet` was a plain ModelViewSet and its serializer left
`status`, `commission_percentage` and the `can_*` flags writable, so the
representative alone could send

    PATCH /api/representatives/{id}/
    {"status": "accepted", "commission_percentage": 100, "can_complete_sale": true}

and get 200. `create_reservation_for_buyer` then computed the commission from
that percentage, so the whole sale went to the representative and the seller
was paid nothing.

The generic create/update endpoints are now closed (405) and the serializer is
output-only. The owner's own paths — accept/ and update_permissions/, which is
itself a PATCH — must keep working, so they are pinned here too.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, Representative, User

pytestmark = pytest.mark.django_db

ESCALATION = {
    'status': 'accepted',
    'commission_percentage': 100,
    'can_complete_sale': True,
    'can_list': True,
    'can_negotiate': True,
}


@pytest.fixture
def owner():
    return User.objects.create_user(username='rep_owner', password='pass12345')


@pytest.fixture
def rep():
    return User.objects.create_user(username='rep_agent', password='pass12345')


@pytest.fixture
def assignment(owner, rep):
    category, _ = Category.objects.get_or_create(name='repcat', slug='repcat')
    item = Item.objects.create(
        owner=owner, title='t', description='d', category=category,
        condition='new', quantity=1, status='active',
    )
    # Flags set explicitly: the model defaults can_complete_sale to True, and
    # the tests need a starting row the escalation would visibly change.
    return Representative.objects.create(
        item=item, owner=owner, representative=rep,
        commission_percentage=Decimal('5'), status='pending',
        can_list=False, can_negotiate=False, can_complete_sale=False,
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def assert_untouched(assignment):
    assignment.refresh_from_db()
    assert assignment.status == 'pending'
    assert assignment.commission_percentage == Decimal('5')
    assert not (assignment.can_list or assignment.can_negotiate
                or assignment.can_complete_sale)


@pytest.mark.parametrize('method', ['patch', 'put'])
def test_representative_cannot_promote_themselves(method, rep, assignment):
    response = getattr(client_for(rep), method)(
        f'/api/representatives/{assignment.id}/', ESCALATION, format='json')

    assert response.status_code == 405
    assert_untouched(assignment)


@pytest.mark.parametrize('method', ['patch', 'put'])
def test_owner_cannot_bypass_the_actions_either(method, owner, assignment):
    # The owner has legitimate ways to change this row; the generic endpoint
    # is not one of them, so state changes always pass the actions' checks.
    response = getattr(client_for(owner), method)(
        f'/api/representatives/{assignment.id}/', ESCALATION, format='json')

    assert response.status_code == 405
    assert_untouched(assignment)


def test_generic_create_is_closed(rep, assignment):
    response = client_for(rep).post('/api/representatives/', {
        'item': assignment.item_id, **ESCALATION}, format='json')

    assert response.status_code == 405
    assert Representative.objects.count() == 1


def test_representative_cannot_accept_their_own_request(rep, assignment):
    response = client_for(rep).post(f'/api/representatives/{assignment.id}/accept/')

    assert response.status_code == 403
    assert_untouched(assignment)


def test_owner_can_still_accept(owner, assignment):
    response = client_for(owner).post(f'/api/representatives/{assignment.id}/accept/')

    assert response.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == 'accepted'
    assert assignment.commission_percentage == Decimal('5')


def test_owner_update_permissions_patch_still_works(owner, assignment):
    assignment.status = 'accepted'
    assignment.save(update_fields=['status'])

    response = client_for(owner).patch(
        f'/api/representatives/{assignment.id}/update_permissions/',
        {'can_complete_sale': True, 'commission_percentage': '7.50'}, format='json')

    assert response.status_code == 200
    assignment.refresh_from_db()
    assert assignment.can_complete_sale is True
    assert assignment.commission_percentage == Decimal('7.50')


def test_representative_cannot_use_update_permissions(rep, assignment):
    assignment.status = 'accepted'
    assignment.save(update_fields=['status'])

    response = client_for(rep).patch(
        f'/api/representatives/{assignment.id}/update_permissions/',
        {'commission_percentage': '100'}, format='json')

    assert response.status_code == 403
    assignment.refresh_from_db()
    assert assignment.commission_percentage == Decimal('5')
