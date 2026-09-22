"""
Favorite/unfavorite toggle on items — mirrors test_reels.py's like/unlike
style (pytest-django + APIClient), since Favorite mirrors ReelLike's model
shape (unique_together get_or_create / filter().delete() toggle). Also
covers FixedPriceListingViewSet.favorites, the "My Favorites" list endpoint.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import Category, Favorite, FixedPriceListing, Item, User, UserCategoryAffinity

pytestmark = pytest.mark.django_db


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='pass12345', **kwargs)


def make_item(owner, **extra):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    defaults = dict(
        title='item', description='d', category=category,
        condition='good', quantity=1, status='active',
    )
    defaults.update(extra)
    return Item.objects.create(owner=owner, **defaults)


def make_listing(owner, *, price=Decimal('100.00'), is_active=True, status='active', title=None, **extra):
    item = make_item(owner, status=status, title=title or f'listing-{price}', **extra)
    return FixedPriceListing.objects.create(
        item=item, price=price, deposit_percentage=Decimal('10.00'), is_active=is_active,
    )


@pytest.fixture
def client():
    return APIClient()


def test_favorite_and_unfavorite_item(client):
    owner = make_user('fav_owner')
    item = make_item(owner)
    buyer = make_user('fav_buyer')
    client.force_authenticate(user=buyer)

    response = client.post(f'/api/items/{item.id}/favorite/')
    assert response.status_code == 200
    assert response.json()['is_favorited'] is True
    assert Favorite.objects.filter(item=item, user=buyer).exists()

    # Favoriting again is idempotent (get_or_create) — no duplicate row.
    response = client.post(f'/api/items/{item.id}/favorite/')
    assert response.status_code == 200
    assert Favorite.objects.filter(item=item, user=buyer).count() == 1

    response = client.post(f'/api/items/{item.id}/unfavorite/')
    assert response.status_code == 200
    assert response.json()['is_favorited'] is False
    assert not Favorite.objects.filter(item=item, user=buyer).exists()


def test_unfavorite_without_a_prior_favorite_is_a_noop(client):
    owner = make_user('fav_owner2')
    item = make_item(owner)
    user = make_user('fav_noop_user')
    client.force_authenticate(user=user)

    response = client.post(f'/api/items/{item.id}/unfavorite/')
    assert response.status_code == 200
    assert response.json()['is_favorited'] is False


def test_favorite_requires_authentication(client):
    owner = make_user('fav_owner3')
    item = make_item(owner)

    response = client.post(f'/api/items/{item.id}/favorite/')
    assert response.status_code in (401, 403)


def test_favorite_state_persists_across_requests_via_is_favorited_field(client):
    """The bug this feature fixes: is_favorited must reflect a prior favorite
    on a totally separate request/serialization, not just the toggle response."""
    owner = make_user('fav_owner4')
    item = make_item(owner)
    buyer = make_user('fav_buyer4')
    client.force_authenticate(user=buyer)

    detail_before = client.get(f'/api/items/{item.id}/')
    assert detail_before.json()['is_favorited'] is False

    client.post(f'/api/items/{item.id}/favorite/')

    detail_after = client.get(f'/api/items/{item.id}/')
    assert detail_after.json()['is_favorited'] is True


def test_favorite_is_per_user(client):
    owner = make_user('fav_owner5')
    item = make_item(owner)
    buyer = make_user('fav_buyer5')
    other_user = make_user('fav_other5')

    client.force_authenticate(user=buyer)
    client.post(f'/api/items/{item.id}/favorite/')

    client.force_authenticate(user=other_user)
    response = client.get(f'/api/items/{item.id}/')
    assert response.json()['is_favorited'] is False


# ---------------------------------------------------------------------------
# Favoriting feeds UserCategoryAffinity, so it factors into `recommended`.
# ---------------------------------------------------------------------------

def test_favoriting_item_boosts_category_affinity(client):
    owner = make_user('aff_owner')
    buyer = make_user('aff_buyer')
    item = make_item(owner)
    client.force_authenticate(user=buyer)

    response = client.post(f'/api/items/{item.id}/favorite/')

    assert response.status_code == 200
    affinity = UserCategoryAffinity.objects.get(user=buyer, category=item.category)
    assert affinity.score == 2


def test_favoriting_same_item_twice_does_not_double_count_affinity(client):
    owner = make_user('aff_owner2')
    buyer = make_user('aff_buyer2')
    item = make_item(owner)
    client.force_authenticate(user=buyer)

    client.post(f'/api/items/{item.id}/favorite/')
    client.post(f'/api/items/{item.id}/favorite/')  # already favorited — idempotent toggle

    affinity = UserCategoryAffinity.objects.get(user=buyer, category=item.category)
    assert affinity.score == 2


def test_refavoriting_after_unfavorite_bumps_affinity_again(client):
    """Unfavorite then favorite again is a fresh, deliberate signal — not
    a toggle bounce — so it should reinforce affinity again, same as buying
    the same category twice would."""
    owner = make_user('aff_owner3')
    buyer = make_user('aff_buyer3')
    item = make_item(owner)
    client.force_authenticate(user=buyer)

    client.post(f'/api/items/{item.id}/favorite/')
    client.post(f'/api/items/{item.id}/unfavorite/')
    client.post(f'/api/items/{item.id}/favorite/')

    affinity = UserCategoryAffinity.objects.get(user=buyer, category=item.category)
    assert affinity.score == 4


def test_unfavoriting_does_not_change_affinity(client):
    owner = make_user('aff_owner4')
    buyer = make_user('aff_buyer4')
    item = make_item(owner)
    client.force_authenticate(user=buyer)

    client.post(f'/api/items/{item.id}/favorite/')
    client.post(f'/api/items/{item.id}/unfavorite/')

    affinity = UserCategoryAffinity.objects.get(user=buyer, category=item.category)
    assert affinity.score == 2


# ---------------------------------------------------------------------------
# "My Favorites" list — /api/listings/fixed/favorites/
# ---------------------------------------------------------------------------

def favorite(user, listing):
    Favorite.objects.create(user=user, item=listing.item)


def test_favorites_list_returns_favorited_listings_most_recent_first(client):
    owner = make_user('list_owner')
    buyer = make_user('list_buyer')
    listing_a = make_listing(owner, title='a')
    listing_b = make_listing(owner, title='b')
    favorite(buyer, listing_a)
    favorite(buyer, listing_b)  # favorited after a — should come first

    client.force_authenticate(user=buyer)
    response = client.get('/api/listings/fixed/favorites/')

    assert response.status_code == 200
    body = response.json()
    ids = [row['id'] for row in body['results']]
    assert ids == [listing_b.id, listing_a.id]


def test_favorites_list_excludes_unfavorited_and_other_users_favorites(client):
    owner = make_user('list_owner2')
    buyer = make_user('list_buyer2')
    other_user = make_user('list_other2')
    mine = make_listing(owner, title='mine')
    unfavorited = make_listing(owner, title='unfavorited')
    someone_elses = make_listing(owner, title='someone-elses')
    favorite(buyer, mine)
    favorite(other_user, someone_elses)

    client.force_authenticate(user=buyer)
    response = client.get('/api/listings/fixed/favorites/')

    ids = [row['id'] for row in response.json()['results']]
    assert ids == [mine.id]
    assert unfavorited.id not in ids
    assert someone_elses.id not in ids


def test_favorites_list_omits_favorited_item_with_no_active_listing(client):
    """Favoriting an item whose fixed listing was later deactivated/removed
    shouldn't crash the endpoint — it should just quietly drop from the list,
    same as `recommended`'s pool handles listings that go inactive."""
    owner = make_user('list_owner3')
    buyer = make_user('list_buyer3')
    inactive = make_listing(owner, title='inactive', is_active=False)
    favorite(buyer, inactive)

    client.force_authenticate(user=buyer)
    response = client.get('/api/listings/fixed/favorites/')

    assert response.status_code == 200
    assert response.json()['results'] == []


def test_favorites_list_requires_authentication(client):
    response = client.get('/api/listings/fixed/favorites/')
    assert response.status_code in (401, 403)


def test_favorites_list_is_empty_for_user_with_no_favorites(client):
    buyer = make_user('list_buyer4')
    client.force_authenticate(user=buyer)

    response = client.get('/api/listings/fixed/favorites/')

    assert response.status_code == 200
    assert response.json()['results'] == []
