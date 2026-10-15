from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import Category, FixedPriceListing, Item, User

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_category(name):
    return Category.objects.create(name=name, slug=name)


def make_listing(owner, category, price, *, condition='used', status='active',
                  rating=Decimal('0.00'), sold_count=0, is_active=True, title=None):
    item = Item.objects.create(
        owner=owner,
        title=title or f'item-{price}',
        description='desc',
        category=category,
        condition=condition,
        status=status,
        rating=rating,
        sold_count=sold_count,
    )
    return FixedPriceListing.objects.create(
        item=item,
        price=price,
        deposit_percentage=Decimal('10.00'),
        is_active=is_active,
    )


def similar_url(listing_id):
    return f'/api/listings/fixed/{listing_id}/similar/'


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def sellers():
    return make_user('seller'), make_user('other_seller')


@pytest.fixture
def categories():
    return make_category('electronics'), make_category('books')


def test_excludes_self_from_results(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'))
    make_listing(other_seller, category, Decimal('105.00'))

    response = client.get(similar_url(target.pk))

    assert response.status_code == 200
    ids = [row['id'] for row in response.data]
    assert target.pk not in ids


def test_excludes_inactive_listings_and_non_active_items(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'))
    make_listing(other_seller, category, Decimal('100.00'), is_active=False)
    make_listing(other_seller, category, Decimal('100.00'), status='draft')
    active_match = make_listing(other_seller, category, Decimal('100.00'))

    response = client.get(similar_url(target.pk))

    ids = [row['id'] for row in response.data]
    assert ids == [active_match.pk]


def test_excludes_same_seller_listings(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'))
    make_listing(seller, category, Decimal('100.00'))
    other_seller_match = make_listing(other_seller, category, Decimal('100.00'))

    response = client.get(similar_url(target.pk))

    ids = [row['id'] for row in response.data]
    assert ids == [other_seller_match.pk]


def test_orders_by_category_then_price_proximity(client, sellers, categories):
    seller, other_seller = sellers
    category, other_category = categories
    target = make_listing(seller, category, Decimal('100.00'))
    close = make_listing(other_seller, category, Decimal('105.00'), title='close')
    far = make_listing(other_seller, category, Decimal('125.00'), title='far')
    other_cat = make_listing(other_seller, other_category, Decimal('100.00'))

    response = client.get(similar_url(target.pk))

    ids = [row['id'] for row in response.data]
    assert ids.index(close.pk) < ids.index(far.pk) < ids.index(other_cat.pk)


def test_condition_and_popularity_break_price_ties(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'), condition='new')
    same_condition_popular = make_listing(
        other_seller, category, Decimal('100.00'),
        condition='new', rating=Decimal('4.80'), sold_count=40, title='best-match',
    )
    different_condition = make_listing(
        other_seller, category, Decimal('100.00'),
        condition='used', rating=Decimal('0.00'), sold_count=0, title='worse-match',
    )

    response = client.get(similar_url(target.pk))

    ids = [row['id'] for row in response.data]
    assert ids.index(same_condition_popular.pk) < ids.index(different_condition.pk)


def test_fallback_fills_missing_results_from_other_categories(client, sellers, categories):
    seller, other_seller = sellers
    category, other_category = categories
    target = make_listing(seller, category, Decimal('100.00'))
    same_cat = make_listing(other_seller, category, Decimal('100.00'))
    popular_other = make_listing(
        other_seller, other_category, Decimal('999.00'),
        rating=Decimal('4.90'), sold_count=50,
    )

    response = client.get(similar_url(target.pk), {'limit': 2})

    ids = [row['id'] for row in response.data]
    assert len(ids) == 2
    assert same_cat.pk in ids
    assert popular_other.pk in ids


def test_limit_query_param_is_respected(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'))
    for i in range(5):
        make_listing(other_seller, category, Decimal('100.00'), title=f'candidate-{i}')

    response = client.get(similar_url(target.pk), {'limit': 3})

    assert len(response.data) == 3


def test_limit_is_clamped_to_max(client, sellers, categories):
    seller, other_seller = sellers
    category, _ = categories
    target = make_listing(seller, category, Decimal('100.00'))
    for i in range(5):
        make_listing(other_seller, category, Decimal('100.00'), title=f'candidate-{i}')

    response = client.get(similar_url(target.pk), {'limit': 9999})

    assert response.status_code == 200
    assert len(response.data) == 5
