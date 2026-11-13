"""Neither party can decide a dispute; only resolve/ (admins) can.

`DisputeViewSet` was a ModelViewSet whose serializer left status, resolution,
resolution_action and admin_notes writable. The seller (defendant) could send

    PATCH /api/disputes/{id}/
    {"status": "resolved", "resolution": "refunded", "admin_notes": "ok by admin"}

and get 200: the complaint left the open-disputes queue the admins work from,
and the buyer saw a "resolution" in the admin's voice.

Generic create/update now answer 405 and the serializer is output-only. The
real paths — create_dispute for the buyer, resolve for an admin — are pinned.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Dispute, Item, User

pytestmark = pytest.mark.django_db

FAKE_DECISION = {
    'status': 'resolved',
    'resolution': 'refunded',
    'resolution_action': 'refund_buyer',
    'admin_notes': 'ok by admin',
}


@pytest.fixture
def buyer():
    return User.objects.create_user(username='dsp_buyer', password='pass12345')


@pytest.fixture
def seller():
    return User.objects.create_user(username='dsp_seller', password='pass12345')


@pytest.fixture
def item(seller):
    category, _ = Category.objects.get_or_create(name='dspcat', slug='dspcat')
    return Item.objects.create(
        owner=seller, title='t', description='d', category=category,
        condition='new', quantity=1, status='active',
    )


@pytest.fixture
def dispute(item, buyer, seller):
    return Dispute.objects.create(item=item, complainant=buyer, defendant=seller,
                                  title='scam', description='never arrived')


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def assert_still_open(dispute):
    dispute.refresh_from_db()
    assert dispute.status == 'open'
    assert not dispute.admin_notes
    assert not dispute.resolution


@pytest.mark.parametrize('method', ['patch', 'put'])
@pytest.mark.parametrize('who', ['seller', 'buyer'])
def test_no_party_can_decide_their_own_dispute(method, who, buyer, seller, dispute):
    user = seller if who == 'seller' else buyer
    response = getattr(client_for(user), method)(
        f'/api/disputes/{dispute.id}/', FAKE_DECISION, format='json')

    assert response.status_code == 405
    assert_still_open(dispute)


def test_generic_create_is_closed(buyer, item):
    response = client_for(buyer).post('/api/disputes/', {
        'item': item.id, 'title': 't', 'description': 'd', **FAKE_DECISION}, format='json')

    assert response.status_code == 405
    assert Dispute.objects.count() == 0


def test_party_cannot_use_resolve(seller, dispute):
    response = client_for(seller).post(f'/api/disputes/{dispute.id}/resolve/', {
        'resolution': 'done', 'resolution_action': 'no_action'}, format='json')

    assert response.status_code == 403
    assert_still_open(dispute)


def test_buyer_can_still_open_a_dispute(buyer, item):
    response = client_for(buyer).post('/api/disputes/create_dispute/', {
        'item_id': item.id, 'title': 'broken', 'description': 'arrived broken',
        'reason': 'damaged'}, format='json')

    assert response.status_code == 201, response.data
    assert Dispute.objects.filter(complainant=buyer, status='open').exists()


def test_admin_can_still_resolve(dispute):
    admin = User.objects.create_user(username='dsp_admin', password='pass12345',
                                     is_staff=True)

    response = client_for(admin).post(f'/api/disputes/{dispute.id}/resolve/', {
        'resolution': 'checked, nothing to refund', 'resolution_action': 'no_action',
        'admin_notes': 'real note'}, format='json')

    assert response.status_code == 200, response.data
    dispute.refresh_from_db()
    assert dispute.status == 'resolved'
    assert dispute.admin_notes == 'real note'
