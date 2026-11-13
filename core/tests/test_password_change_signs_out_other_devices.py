"""Changing or resetting the password ends every other session.

A refresh token lives 7 days and was never tied to the password, so whoever
had stolen one stayed inside the account after its owner changed the
password. Each of the three paths now blacklists every outstanding refresh
token; change-password also hands the device that made the change a new pair
so its owner is not signed out of their own phone.
"""
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import User

pytestmark = pytest.mark.django_db

OLD = 'Old-Passw0rd!x'
NEW = 'Brand-new-Pass9'
PHONE = '07701234567'
E164 = '+9647701234567'


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user():
    return User.objects.create_user(username='sess_user', password=OLD, phone=E164,
                                    email='sess@example.com')


def refresh_works(token):
    response = APIClient().post(reverse('token_refresh'), {'refresh': token}, format='json')
    return response.status_code == 200


def stolen_token(user):
    return str(RefreshToken.for_user(user))


def test_change_password_signs_out_other_devices_but_keeps_this_one(user):
    thief = stolen_token(user)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post('/api/auth/change-password/', {
        'old_password': OLD, 'new_password': NEW, 'new_password2': NEW}, format='json')

    assert response.status_code == 200, response.data
    assert not refresh_works(thief)
    assert refresh_works(response.data['refresh'])
    assert response.data['access']


def test_whatsapp_reset_signs_out_every_device(user):
    thief = stolen_token(user)
    client = APIClient()
    with patch('core.otp_views.send_otp', return_value='msg-1'):
        client.post(reverse('phone-send-code'), {'phone': PHONE}, format='json')
    with patch('core.otp_views.verify_otp', return_value=True):
        response = client.post(reverse('reset-password-code'), {
            'phone': PHONE, 'code': '123456', 'new_password': NEW}, format='json')

    assert response.status_code == 200, response.data
    user.refresh_from_db()
    assert user.check_password(NEW)
    assert not refresh_works(thief)


def test_email_reset_signs_out_every_device(user):
    thief = stolen_token(user)
    with patch.object(User, 'check_verification_code', return_value=True):
        response = APIClient().post(reverse('reset-password-confirm'), {
            'email': user.email, 'code': '123456',
            'new_password': NEW, 'new_password2': NEW}, format='json')

    assert response.status_code == 200, response.data
    assert not refresh_works(thief)


def test_a_wrong_old_password_revokes_nothing(user):
    session = stolen_token(user)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post('/api/auth/change-password/', {
        'old_password': 'not-it', 'new_password': NEW, 'new_password2': NEW}, format='json')

    assert response.status_code == 400
    assert refresh_works(session)
