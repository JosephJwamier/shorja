"""
A seller must have somewhere for the courier to collect from before they can
list anything.

Without this, item creation succeeded and the failure surfaced much later as
a DeliveryOrder with an empty from_address (driver dispatched to nothing)
and a fee that silently fell back to the inter-governorate rate.

The companion rule is that a seller's first location becomes their primary
one: otherwise "add a primary location" is unsatisfiable through the app,
since nothing marks a lone location as primary.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import Category, Location, User, UserStore

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def make_seller(username='sel'):
    return User.objects.create_user(username=username, password='x', phone='07700000000')


def location_payload(name='home', primary=None):
    payload = {
        'name': name, 'address_line1': 'street 1', 'city': 'بغداد', 'state': 'بغداد',
        'country': 'Iraq', 'contact_person': 'p', 'contact_phone': '07701112233',
        'location_type': 'user_address',
    }
    if primary is not None:
        payload['is_primary'] = primary
    return payload


def item_payload():
    category = Category.objects.get_or_create(name='cat', slug='cat')[0]
    return {
        'title': 't', 'description': 'd', 'category': category.id,
        'condition': 'new', 'quantity': 1,
    }


# ---------------------------------------------------------------------------
# First location becomes primary
# ---------------------------------------------------------------------------

def test_first_location_is_made_primary(client):
    seller = make_seller('fp_1')
    client.force_authenticate(user=seller)

    response = client.post('/api/locations/', location_payload(), format='json')

    assert response.status_code == 201, response.data
    assert Location.objects.get(user=seller).is_primary is True


def test_second_location_is_not_silently_promoted(client):
    seller = make_seller('fp_2')
    client.force_authenticate(user=seller)
    client.post('/api/locations/', location_payload('first'), format='json')

    client.post('/api/locations/', location_payload('second'), format='json')

    assert Location.objects.get(user=seller, name='first').is_primary is True
    assert Location.objects.get(user=seller, name='second').is_primary is False


def test_an_explicit_primary_on_the_first_location_is_respected(client):
    seller = make_seller('fp_3')
    client.force_authenticate(user=seller)

    client.post('/api/locations/', location_payload('only', primary=True), format='json')

    assert Location.objects.get(user=seller).is_primary is True


# ---------------------------------------------------------------------------
# Item creation is gated on having one
# ---------------------------------------------------------------------------

def test_creating_an_item_without_a_location_is_refused(client):
    seller = make_seller('gate_1')
    client.force_authenticate(user=seller)

    response = client.post('/api/items/', item_payload(), format='json')

    assert response.status_code == 400
    # A machine-readable code so the app can open the location sheet rather
    # than parse an Arabic sentence.
    assert response.data['code'] == 'no_pickup_location'
    assert 'الموقع' in response.data['message'] or 'موقع' in response.data['message']


def test_creating_an_item_succeeds_once_a_location_exists(client):
    seller = make_seller('gate_2')
    client.force_authenticate(user=seller)
    assert client.post('/api/locations/', location_payload(), format='json').status_code == 201

    response = client.post('/api/items/', item_payload(), format='json')

    assert response.status_code == 201, response.data


def test_a_store_default_location_is_enough_on_its_own(client):
    # The store's declared shipping point is what resolve_pickup prefers, so
    # a seller who set one is not missing anything.
    seller = make_seller('gate_3')
    branch = Location.objects.create(
        user=seller, name='branch', address_line1='x', city='أربيل', state='أربيل',
        country='Iraq', contact_person='p', contact_phone='1', is_primary=False,
    )
    UserStore.objects.create(user=seller, name='s', default_location=branch)
    client.force_authenticate(user=seller)

    response = client.post('/api/items/', item_payload(), format='json')

    assert response.status_code == 201, response.data


def test_a_deactivated_location_does_not_count(client):
    seller = make_seller('gate_4')
    Location.objects.create(
        user=seller, name='old', address_line1='x', city='بغداد', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1', is_active=False,
    )
    client.force_authenticate(user=seller)

    response = client.post('/api/items/', item_payload(), format='json')

    assert response.status_code == 400
    assert response.data['code'] == 'no_pickup_location'
