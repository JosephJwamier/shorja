"""Store badges: admin-only grants with caps, the automatic verified badge,
the derived performance level, what the API exposes, and reward coupons."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, Location, Reservation, StoreBadge, StoreCoupon,
    StoreFollow, User, UserStore,
)
from core.services.store_badges import (
    BadgeError, badges_for_store, grant_badge, grant_reward_coupon, level_for, recompute_level,
    revoke_badge, sync_verified_badge,
)
from core.tasks import recompute_store_levels

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def make_store(username, **user_flags):
    user = User.objects.create_user(username=username, password='x', **user_flags)
    return UserStore.objects.create(user=user, name=username + ' store')


def admin_user():
    return User.objects.create_superuser(username='root', password='x', email='r@x.io')


# ---------------------------------------------------------------------------
# Granted badges
# ---------------------------------------------------------------------------

def test_admin_only_badges_need_an_admin():
    store = make_store('g1')
    with pytest.raises(BadgeError):
        grant_badge(store, StoreBadge.KIND_FOUNDER)
    badge = grant_badge(store, StoreBadge.KIND_FOUNDER, by=admin_user())
    assert badge.is_active and badge.granted_by.username == 'root'
    assert badges_for_store(store) == ['founder']


def test_grant_is_idempotent_and_revoke_keeps_history():
    store, root = make_store('g2'), admin_user()
    first = grant_badge(store, StoreBadge.KIND_PIONEER, by=root)
    assert grant_badge(store, StoreBadge.KIND_PIONEER, by=root).id == first.id
    revoke_badge(store, StoreBadge.KIND_PIONEER, by=root, note='خطأ')
    assert badges_for_store(store) == []
    assert StoreBadge.objects.filter(store=store).count() == 1
    again = grant_badge(store, StoreBadge.KIND_PIONEER, by=root)
    assert again.id != first.id and StoreBadge.objects.filter(store=store).count() == 2


def test_founder_cap_is_enforced(settings):
    root = admin_user()
    StoreBadge.CAPS[StoreBadge.KIND_FOUNDER] = 2  # keep the test fast
    try:
        grant_badge(make_store('f1'), StoreBadge.KIND_FOUNDER, by=root)
        grant_badge(make_store('f2'), StoreBadge.KIND_FOUNDER, by=root)
        with pytest.raises(BadgeError):
            grant_badge(make_store('f3'), StoreBadge.KIND_FOUNDER, by=root)
        # A revoked seat frees up.
        revoke_badge(UserStore.objects.get(name='f1 store'), StoreBadge.KIND_FOUNDER, by=root)
        assert grant_badge(make_store('f4'), StoreBadge.KIND_FOUNDER, by=root).is_active
    finally:
        StoreBadge.CAPS[StoreBadge.KIND_FOUNDER] = 50


def test_verified_badge_follows_the_owners_flags():
    store = make_store('v1')
    assert sync_verified_badge(store) is None and badges_for_store(store) == []
    user = store.user
    user.identity_verified = True
    user.phone_verified = True
    user.save()  # the post_save signal grants it
    assert badges_for_store(store) == ['verified']
    user.phone_verified = False
    user.save()
    assert badges_for_store(store) == []


# ---------------------------------------------------------------------------
# Derived level
# ---------------------------------------------------------------------------

def stats(sales, rating, followers):
    return {'sales': sales, 'rating': Decimal(str(rating)), 'followers': followers}


def test_level_ladder_is_monotonic():
    assert level_for(stats(0, 5.0, 1000)) == ''
    assert level_for(stats(1, 0, 0)) == 'bronze'
    assert level_for(stats(20, 4.0, 0)) == 'silver'
    assert level_for(stats(100, 4.3, 100)) == 'gold'
    assert level_for(stats(400, 4.6, 500)) == 'diamond'
    # Sales alone never skip a rung: 400 sales at rating 3.9 is bronze.
    assert level_for(stats(400, 3.9, 500)) == 'bronze'


def test_thresholds_can_be_tuned_from_settings(settings):
    settings.STORE_LEVEL_THRESHOLDS = {'silver': {'sales': 5}}
    assert level_for(stats(5, 4.0, 0)) == 'silver'


def make_confirmed_sale(store, buyer, released_at):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=store.user, title='t', description='d', category=category,
                               condition='new', quantity=1, status='active')
    FixedPriceListing.objects.create(item=item, price=Decimal('100'), deposit_percentage=10)
    return Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10'), total_price=Decimal('100'), status='confirmed',
        expires_at=timezone.now() + timedelta(days=1), completed_at=released_at,
        payment_released_to_seller=True, payment_released_at=released_at,
    )


def test_recompute_reads_real_sales_rating_and_followers():
    store = make_store('lv1')
    buyer = User.objects.create_user(username='lv1_buyer', password='x')
    now = timezone.now()
    make_confirmed_sale(store, buyer, now - timedelta(days=3))
    make_confirmed_sale(store, buyer, now - timedelta(days=120))  # outside the 90-day window
    assert recompute_level(store) == ('', 'bronze')
    assert UserStore.objects.get(pk=store.pk).level == 'bronze'


def test_nightly_task_sweeps_every_active_store():
    a, b = make_store('sw1'), make_store('sw2')
    buyer = User.objects.create_user(username='sw_buyer', password='x')
    make_confirmed_sale(a, buyer, timezone.now())
    b.is_active = False; b.save()
    recompute_store_levels()
    assert UserStore.objects.get(pk=a.pk).level == 'bronze'
    assert UserStore.objects.get(pk=b.pk).level == ''


# ---------------------------------------------------------------------------
# API exposure
# ---------------------------------------------------------------------------

def test_store_payload_carries_badges_in_display_order(client):
    store, root = make_store('api1'), admin_user()
    grant_badge(store, StoreBadge.KIND_PIONEER, by=root)
    store.user.identity_verified = store.user.phone_verified = True
    store.user.save()
    store.level = 'gold'; store.save()
    client.force_authenticate(user=store.user)
    response = client.get('/api/stores/my_store/')
    assert response.status_code == 200, response.data
    data = response.data.get('data', response.data)
    assert data['badges'] == ['pioneer', 'verified', 'gold']
    assert data['level'] == 'gold'


def test_seller_on_an_item_carries_store_badges(client):
    store, root = make_store('api2'), admin_user()
    grant_badge(store, StoreBadge.KIND_FOUNDER, by=root)
    buyer = User.objects.create_user(username='api2_buyer', password='x')
    reservation = make_confirmed_sale(store, buyer, timezone.now())
    client.force_authenticate(user=buyer)
    response = client.get('/api/items/%d/' % reservation.item_id)
    assert response.status_code == 200
    data = response.data.get('data', response.data)
    assert data['owner']['store_badges'] == ['founder']


def test_badges_cannot_be_written_through_the_store_endpoint(client):
    store = make_store('api3')
    client.force_authenticate(user=store.user)
    client.patch('/api/stores/%d/' % store.id, {'level': 'diamond', 'badges': ['founder']}, format='json')
    store.refresh_from_db()
    assert store.level == '' and badges_for_store(store) == []


# ---------------------------------------------------------------------------
# Reward coupons
# ---------------------------------------------------------------------------

def test_reward_coupon_is_collectable_and_not_stacked():
    store, root = make_store('rc1'), admin_user()
    coupon = grant_reward_coupon(store, by=root)
    assert coupon.code.startswith('GIFT-') and coupon.amount == Decimal('5000.00')
    assert coupon.expires_at > timezone.now() + timedelta(days=29)
    assert coupon.is_valid
    assert grant_reward_coupon(store, by=root) is None, 'one live reward at a time'
    assert StoreCoupon.objects.filter(store=store).count() == 1
