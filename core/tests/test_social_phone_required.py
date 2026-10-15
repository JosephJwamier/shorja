"""Google sign-in cannot finish without a verified phone.

Google never tells us the number — its ID token carries no phone_number
claim — so the client collects it and proves it with a Firebase Phone Auth
token. SocialLoginView therefore answers 'phone_required' and writes
nothing; SocialAttachPhoneView takes both tokens and creates the account.

Firebase is mocked at the firebase_admin boundary: no network.
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from core.models import User

pytestmark = pytest.mark.django_db

GOOGLE_TOKEN = 'google-tok'
PHONE_TOKEN = 'phone-tok'

GOOGLE_CLAIMS = {
    'uid': 'google-uid-1',
    'email': 'Ali@Example.com',
    'name': 'علي',
    'email_verified': True,
}


def firebase(phone_number='+9647701234567', google=None):
    """One decoder for both tokens: the endpoint verifies a Google token and
    a phone token through the same call, so the mock has to answer by which
    token string it was handed."""
    claims = {**GOOGLE_CLAIMS, **(google or {})}

    def decode(token, *args, **kwargs):
        if token == PHONE_TOKEN:
            return {'uid': 'phone-uid', 'phone_number': phone_number}
        return claims

    return (
        patch('core.firebase.get_firebase_app', return_value=object()),
        patch('firebase_admin.auth.verify_id_token', side_effect=decode),
    )


# Migration 0045 seeds a support account into every database, test ones
# included — so a bare User.objects.count() is never zero.
SUPPORT_USERNAME = 'الدعم_الفني'


def accounts():
    """Real accounts: everything but the seeded support user."""
    return User.objects.exclude(username=SUPPORT_USERNAME)


@pytest.fixture
def client():
    return APIClient()


def social_login(client, **kw):
    app_patch, verify_patch = firebase(**kw)
    with app_patch, verify_patch:
        return client.post('/api/auth/social-login/',
                           {'id_token': GOOGLE_TOKEN}, format='json')


def attach(client, phone_id_token=PHONE_TOKEN, **kw):
    app_patch, verify_patch = firebase(**kw)
    with app_patch, verify_patch:
        return client.post('/api/auth/social-login/attach-phone/',
                           {'id_token': GOOGLE_TOKEN,
                            'phone_id_token': phone_id_token}, format='json')


class TestFirstStepRefusesAndWritesNothing:
    def test_a_new_google_identity_is_asked_for_a_phone(self, client):
        r = social_login(client)
        assert r.status_code == 200, r.data
        assert r.data['status'] == 'phone_required'
        assert r.data['is_new_user'] is True

    def test_no_account_is_created_yet(self, client):
        social_login(client)
        # The whole reason this step writes nothing: an abandoned signup
        # must not leave behind a row nobody can sign in to or recover.
        assert accounts().count() == 0

    def test_no_session_tokens_are_handed_out(self, client):
        assert 'tokens' not in social_login(client).data

    def test_an_existing_account_without_a_verified_phone_is_also_asked(self, client):
        User.objects.create_user(username='ali', password='x',
                                 email='ali@example.com')
        r = social_login(client)
        assert r.data['status'] == 'phone_required'
        assert r.data['is_new_user'] is False

    def test_an_account_with_a_verified_phone_signs_straight_in(self, client):
        User.objects.create_user(username='ali', password='x',
                                 email='ali@example.com',
                                 phone='07701234567', phone_verified=True)
        r = social_login(client)
        assert r.status_code == 200, r.data
        assert 'tokens' in r.data
        assert r.data.get('status') != 'phone_required'


class TestSecondStepCompletesTheSignup:
    def test_account_is_created_with_the_phone_already_verified(self, client):
        r = attach(client)
        assert r.status_code == 200, r.data
        assert 'tokens' in r.data
        assert r.data['is_new_user'] is True

        user = accounts().get()
        assert user.phone == '+9647701234567'
        assert user.phone_verified is True
        assert user.email == 'ali@example.com'      # lowercased on save
        assert user.firebase_uid == 'google-uid-1'

    def test_the_new_account_has_no_usable_password(self, client):
        attach(client)
        assert accounts().get().has_usable_password() is False

    def test_signing_in_again_reuses_the_same_account(self, client):
        attach(client)
        r = social_login(client)
        assert 'tokens' in r.data
        assert accounts().count() == 1

    def test_an_existing_password_account_is_linked_not_duplicated(self, client):
        existing = User.objects.create_user(
            username='ali', password='x', email='ali@example.com')
        r = attach(client)
        assert r.status_code == 200, r.data
        assert r.data['is_new_user'] is False
        assert accounts().count() == 1
        existing.refresh_from_db()
        assert existing.phone == '+9647701234567'
        assert existing.phone_verified is True


class TestWhatTheSecondStepRefuses:
    def test_both_tokens_are_required(self, client):
        app_patch, verify_patch = firebase()
        with app_patch, verify_patch:
            r = client.post('/api/auth/social-login/attach-phone/',
                            {'id_token': GOOGLE_TOKEN}, format='json')
        assert r.status_code == 400

    def test_a_google_token_cannot_stand_in_for_the_sms_step(self, client):
        # Passing the Google token twice: it carries no phone_number claim,
        # so there is no number to attach and nothing is created.
        r = attach(client, phone_id_token=GOOGLE_TOKEN)
        assert r.status_code == 400
        assert accounts().count() == 0

    def test_a_number_already_held_by_another_account_is_refused(self, client):
        User.objects.create_user(username='someone', password='x',
                                 phone='07701234567', phone_verified=True)
        r = attach(client)
        assert r.status_code == 409
        # And the other account keeps its number.
        assert User.objects.get(username='someone').phone == '+9647701234567'

    def test_an_unusable_number_is_refused(self, client):
        r = attach(client, phone_number='+964740123456')
        assert r.status_code == 400
        assert accounts().count() == 0
