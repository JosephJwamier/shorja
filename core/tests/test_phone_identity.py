"""The phone as the account's identity: unique, canonical, required at signup;
email optional; login by username, email or phone.
"""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def signup_payload(**overrides):
    body = {
        'username': 'tajir', 'first_name': 'عبد', 'last_name': 'الرحمن',
        'phone': '07701234567', 'password': 'Passw0rd!x', 'password2': 'Passw0rd!x',
    }
    body.update(overrides)
    return body


class TestModelInvariant:
    def test_phone_is_stored_canonically_whatever_was_typed(self):
        u = User.objects.create_user(username='a', password='x', phone='0770 123 4567')
        u.refresh_from_db()
        assert u.phone == '+9647701234567'

    def test_blank_phone_becomes_null_so_many_accounts_can_lack_one(self):
        # '' would collide on the second row: MySQL allows many NULLs in a
        # unique index but only one empty string.
        a = User.objects.create_user(username='a', password='x')
        b = User.objects.create_user(username='b', password='x')
        assert a.phone is None and b.phone is None

    def test_blank_email_becomes_null_too(self):
        a = User.objects.create_user(username='a', password='x')
        b = User.objects.create_user(username='b', password='x')
        assert a.email is None and b.email is None

    def test_email_is_lowercased(self):
        u = User.objects.create_user(username='a', password='x', email='  Ali@Example.COM ')
        assert u.email == 'ali@example.com'

    def test_unparseable_phone_raises_rather_than_vanishing(self):
        with pytest.raises(ValueError):
            User.objects.create_user(username='a', password='x', phone='12345')

    def test_two_spellings_of_one_phone_cannot_both_be_saved(self):
        from django.db import IntegrityError
        User.objects.create_user(username='a', password='x', phone='07701234567')
        with pytest.raises(IntegrityError):
            User.objects.create_user(username='b', password='x', phone='+964 770 123 4567')


class TestRegistration:
    def test_signup_without_email_succeeds(self, client):
        r = client.post(reverse('register'), signup_payload(), format='json')
        assert r.status_code == 201, r.data
        assert User.objects.get(username='tajir').email is None

    def test_signup_response_asks_for_phone_not_email_when_no_email(self, client):
        r = client.post(reverse('register'), signup_payload(), format='json')
        assert 'phone' in r.data['message'].lower()

    def test_signup_with_email_still_asks_to_verify_it(self, client):
        r = client.post(reverse('register'),
                        signup_payload(email='ali@example.com'), format='json')
        assert r.status_code == 201, r.data
        assert 'email' in r.data['message'].lower()

    def test_signup_without_phone_is_rejected(self, client):
        body = signup_payload()
        del body['phone']
        r = client.post(reverse('register'), body, format='json')
        assert r.status_code == 400
        assert 'phone' in r.data

    @pytest.mark.parametrize('bad', ['07401234567', '0770123456', 'abcd', '017184512'])
    def test_signup_rejects_a_number_that_cannot_receive_an_sms(self, client, bad):
        r = client.post(reverse('register'), signup_payload(phone=bad), format='json')
        assert r.status_code == 400
        assert 'phone' in r.data

    def test_phone_stored_canonically_from_signup(self, client):
        client.post(reverse('register'), signup_payload(phone='0770 123 4567'), format='json')
        assert User.objects.get(username='tajir').phone == '+9647701234567'

    def test_a_number_already_taken_is_a_field_error_not_a_500(self, client):
        User.objects.create_user(username='first', password='x', phone='07701234567')
        # Typed differently — the canonical comparison must still catch it.
        r = client.post(reverse('register'),
                        signup_payload(phone='+9647701234567'), format='json')
        assert r.status_code == 400
        assert 'phone' in r.data


class TestLogin:
    @pytest.fixture
    def user(self):
        return User.objects.create_user(
            username='tajir', password='Passw0rd!x',
            phone='07701234567', email='ali@example.com')

    @pytest.mark.parametrize('identifier', [
        'tajir',                 # username
        'ali@example.com',       # email
        '07701234567',           # phone as typed at signup
        '+9647701234567',        # phone, international
        '7701234567',            # phone, no leading zero
    ])
    def test_every_identifier_reaches_the_same_account(self, client, user, identifier):
        r = client.post(reverse('login'),
                        {'username': identifier, 'password': 'Passw0rd!x'}, format='json')
        assert r.status_code == 200, r.data
        assert r.data['user']['username'] == 'tajir'

    def test_an_unknown_email_is_401_not_500(self, client, user):
        # Previously left `username` unbound and raised UnboundLocalError.
        r = client.post(reverse('login'),
                        {'username': 'nobody@example.com', 'password': 'x'}, format='json')
        assert r.status_code == 401

    def test_a_real_username_still_wins_over_a_phone_lookup(self, client):
        # Someone whose username happens to look like a number must keep
        # logging in with it.
        named = User.objects.create_user(username='07701234567', password='Passw0rd!x')
        User.objects.create_user(username='other', password='Other!123', phone='07701234567')
        r = client.post(reverse('login'),
                        {'username': '07701234567', 'password': 'Passw0rd!x'}, format='json')
        assert r.status_code == 200, r.data
        assert r.data['user']['username'] == named.username


class TestPasswordResetByEmailNoLongerCrashes:
    def test_blank_email_does_not_match_the_phone_only_accounts(self, client):
        User.objects.create_user(username='a', password='x', phone='07701234567')
        User.objects.create_user(username='b', password='x', phone='07801234567')
        # Both have email NULL. A blank request must be a quiet no-match,
        # not MultipleObjectsReturned.
        r = client.post(reverse('reset-password'), {'email': ''}, format='json')
        assert r.status_code == 200
        assert 'message' in r.data
