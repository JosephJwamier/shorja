"""اسم المتجر يصل مع المالك، فيعرضه التطبيق بدل اسم المستخدم."""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, User, UserStore

pytestmark = pytest.mark.django_db


def owner_of_item(store=None):
    owner = User.objects.create_user(username='sn_owner', password='pass12345')
    if store:
        UserStore.objects.create(user=owner, **store)
    category = Category.objects.create(name='sncat', slug='sncat')
    item = Item.objects.create(
        owner=owner, title='t', description='d', category=category,
        condition='new', quantity=1, status='active')
    body = APIClient().get(f'/api/items/{item.id}/').json()
    return body.get('data', body)['owner']


def test_the_store_name_comes_with_the_owner():
    owner = owner_of_item({'name': 'متجر النور', 'is_active': True})
    assert owner['store_name'] == 'متجر النور'
    assert owner['username'] == 'sn_owner'


def test_no_store_means_an_empty_name():
    assert owner_of_item()['store_name'] == ''


def test_an_inactive_store_is_not_named():
    assert owner_of_item({'name': 'مغلق', 'is_active': False})['store_name'] == ''
