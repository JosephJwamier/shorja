"""
core.tasks.decay_category_affinities — the scheduled weekly decay that keeps
UserCategoryAffinity from being a monotonically-growing counter forever.
record_interest() only ever adds (see models.py); this task is what lets a
customer's old interests fade so newer behavior can outrank them.
"""
import pytest

from core.models import Category, User, UserCategoryAffinity
from core.tasks import decay_category_affinities

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_category(name):
    category, _ = Category.objects.get_or_create(name=name, slug=name)
    return category


def make_affinity(user, category, score):
    return UserCategoryAffinity.objects.create(user=user, category=category, score=score)


def test_decay_multiplies_score_by_factor():
    user = make_user('decay_user1')
    category = make_category('electronics')
    make_affinity(user, category, score=100)

    decay_category_affinities(factor=0.9)

    affinity = UserCategoryAffinity.objects.get(user=user, category=category)
    assert affinity.score == 90


def test_decay_truncates_rather_than_rounds():
    user = make_user('decay_user2')
    category = make_category('books')
    make_affinity(user, category, score=15)  # 15 * 0.9 = 13.5

    decay_category_affinities(factor=0.9)

    affinity = UserCategoryAffinity.objects.get(user=user, category=category)
    assert affinity.score == 13


def test_decay_deletes_rows_that_hit_zero():
    user = make_user('decay_user3')
    category = make_category('toys')
    make_affinity(user, category, score=1)  # 1 * 0.9 truncates to 0

    decay_category_affinities(factor=0.9)

    assert not UserCategoryAffinity.objects.filter(user=user, category=category).exists()


def test_repeated_decay_eventually_clears_a_stale_interest():
    """The actual scenario this exists for: an old interest that nothing is
    reinforcing anymore should fade out over successive runs instead of
    sitting at its peak score forever."""
    user = make_user('decay_user4')
    category = make_category('stale-interest')
    make_affinity(user, category, score=20)

    for _ in range(40):  # ~40 weeks of no reinforcement
        decay_category_affinities(factor=0.9)

    assert not UserCategoryAffinity.objects.filter(user=user, category=category).exists()


def test_decay_does_not_touch_other_users_or_categories():
    user_a = make_user('decay_user5a')
    user_b = make_user('decay_user5b')
    cat_a = make_category('cat-a')
    cat_b = make_category('cat-b')
    make_affinity(user_a, cat_a, score=50)
    make_affinity(user_b, cat_b, score=50)

    decay_category_affinities(factor=0.9)

    assert UserCategoryAffinity.objects.get(user=user_a, category=cat_a).score == 45
    assert UserCategoryAffinity.objects.get(user=user_b, category=cat_b).score == 45


def test_decay_with_no_affinities_is_a_noop():
    result = decay_category_affinities(factor=0.9)
    assert 'Decayed 0' in result
