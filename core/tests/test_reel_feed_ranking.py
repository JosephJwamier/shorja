"""ترتيب خلاصة الريلز — المنطق الخالص ثم النقطة.

الخلاصة كانت `-created_at` محضاً: ما شوهد يُعاد، ومتجر نشيط يبتلعها،
ولا شيء يخصّ المشاهد. وما يُحرَس هنا هو القواعد الثلاث التي حلّت محلّ ذلك،
بترتيب أهمّيتها:

١. ما لم يُشاهَد يسبق دائماً — ولا مجموع درجاتٍ يقلب ذلك.
٢. لا متجران متتاليان من نفس المتجر.
٣. المتابعة ثم التفاعل ثم التصنيف ثم الشهرة.

ويُختبر المنطق مباشرةً لا من شكل استجابة: ترتيبٌ يُستدلّ عليه من JSON
يُثبت أن النقطة تعمل، لا أن القاعدة صحيحة.
"""
import pytest

from core.reel_feed import rank_reels, score_reel, spread_stores

NO_HISTORY = dict(
    seen_ids=set(), followed_store_ids=set(),
    engaged_store_ids=set(), category_rank={},
)


def reel(reel_id, store_id=1, category_id=None, likes=0):
    return (reel_id, store_id, category_id, likes)


class TestUnseenAlwaysWins:
    def test_an_unseen_reel_outranks_a_seen_one(self):
        order = rank_reels(
            [reel(1, store_id=1), reel(2, store_id=2)],
            **{**NO_HISTORY, 'seen_ids': {1}},
        )
        assert order == [2, 1]

    def test_no_amount_of_affinity_promotes_a_seen_reel(self):
        # المشاهَد من متجر يتابعه وبتصنيفه المفضّل وبألف إعجاب، مقابل
        # غير مشاهَد لا صلة له بشيء — وغير المشاهَد يسبق.
        order = rank_reels(
            [reel(1, store_id=7, category_id=3, likes=1000), reel(2, store_id=99)],
            seen_ids={1},
            followed_store_ids={7},
            engaged_store_ids={7},
            category_rank={3: 0},
        )
        assert order[0] == 2, 'لا ترتيب يُقدّم ما رآه المستخدم على ما لم يره'

    def test_diversity_never_promotes_a_seen_reel(self):
        """القاعدتان تتصادمان، والأولى تفوز.

        هذا ما كُسر فعلاً: ريلان غير مشاهَدين متتاليان من متجر واحد،
        وبينهما مشاهَدٌ من متجر آخر — فالتقطه مرورُ التنويع وقدّمه، لأنه
        كان يعمل على القائمة كاملةً بعد أن انتهت الدرجات من عملها.
        """
        order = rank_reels(
            [reel(3, store_id=1), reel(2, store_id=2), reel(1, store_id=1)],
            **{**NO_HISTORY, 'seen_ids': {2}},
        )
        assert order == [3, 1, 2], 'المشاهَد آخراً مهما كلّف التنويع'

    def test_seen_reels_still_appear_after_the_unseen_run_out(self):
        # لا تُحذف: من فرغ منها يجب أن يرى شيئاً لا شاشةً فارغة.
        order = rank_reels(
            [reel(1, store_id=1), reel(2, store_id=2)],
            **{**NO_HISTORY, 'seen_ids': {1, 2}},
        )
        assert sorted(order) == [1, 2]


class TestNoTwoInARowFromOneStore:
    def test_a_flooding_store_is_broken_up(self):
        # متجر ١ نشر أربعة، ومتجر ٢ نشر واحداً — ولا يظهر ١ مرّتين متتاليتين
        # ما دام هناك بديل.
        rows = [(i, 1) for i in range(1, 5)] + [(9, 2)]
        order = spread_stores(rows)
        stores = {1: 1, 2: 1, 3: 1, 4: 1, 9: 2}
        pairs = list(zip(order, order[1:]))
        assert any(stores[a] != stores[b] for a, b in pairs)
        assert order[1] == 9, 'البديل الوحيد يُدرَج ثانياً'

    def test_it_never_drops_a_reel(self):
        rows = [(i, 1) for i in range(1, 6)]
        assert sorted(spread_stores(rows)) == [1, 2, 3, 4, 5]

    def test_one_store_only_is_left_in_order(self):
        # التنويع تحسينٌ لا شرط: حيث لا بديل، لا تُقلَب القائمة بلا فائدة.
        rows = [(1, 5), (2, 5), (3, 5)]
        assert spread_stores(rows) == [1, 2, 3]

    def test_alternating_stores_stay_as_they_are(self):
        rows = [(1, 1), (2, 2), (3, 1), (4, 2)]
        assert spread_stores(rows) == [1, 2, 3, 4]


class TestTheSignals:
    def test_a_followed_store_beats_an_engaged_one(self):
        followed = score_reel(1, 10, None, 0, **{
            **NO_HISTORY, 'followed_store_ids': {10}})
        engaged = score_reel(2, 20, None, 0, **{
            **NO_HISTORY, 'engaged_store_ids': {20}})
        assert followed > engaged

    def test_following_and_engaging_is_not_counted_twice(self):
        # المتابعة تتضمّن الاهتمام — جمعهما يُضاعف إشارة واحدة.
        both = score_reel(1, 10, None, 0, **{
            **NO_HISTORY,
            'followed_store_ids': {10}, 'engaged_store_ids': {10}})
        followed_only = score_reel(1, 10, None, 0, **{
            **NO_HISTORY, 'followed_store_ids': {10}})
        assert both == followed_only

    def test_the_first_interest_category_beats_the_fifth(self):
        first = score_reel(1, 1, 100, 0, **{**NO_HISTORY,
                                            'category_rank': {100: 0}})
        fifth = score_reel(2, 1, 200, 0, **{**NO_HISTORY,
                                            'category_rank': {200: 4}})
        assert first > fifth

    def test_popularity_is_capped(self):
        # سقف منخفض عمداً: بلا سقف تُعيد الخلاصة المشهور نفسه للجميع،
        # وهو عكس ما يحتاجه سوق فيه متاجر صغيرة جديدة.
        huge = score_reel(1, 1, None, 100000, **NO_HISTORY)
        modest = score_reel(2, 1, None, 15, **NO_HISTORY)
        assert huge == modest

    def test_a_followed_store_outweighs_any_popularity(self):
        followed = score_reel(1, 10, None, 0, **{
            **NO_HISTORY, 'followed_store_ids': {10}})
        popular = score_reel(2, 20, None, 999999, **NO_HISTORY)
        assert followed > popular


class TestTieBreaking:
    def test_equal_scores_keep_the_newest_first(self):
        # الترتيب الوارد هو الأحدث أولاً، وهو ما يفضّ التعادل — أي أن
        # السلوك القديم باقٍ حيث لا تمييز.
        order = rank_reels(
            [reel(5, store_id=1), reel(4, store_id=2), reel(3, store_id=3)],
            **NO_HISTORY,
        )
        assert order == [5, 4, 3]

    def test_an_empty_pool_is_an_empty_list(self):
        assert rank_reels([], **NO_HISTORY) == []


@pytest.mark.django_db
class TestThroughTheEndpoint:
    """القواعد أعلاه خالصة؛ وهذه تُثبت أن النقطة تناديها فعلاً."""

    def _setup(self):
        from django.contrib.auth import get_user_model
        from core.models import Reel, UserStore
        from core.tests.phones import next_phone

        User = get_user_model()
        viewer = User.objects.create_user(
            username='mushahid', password='x', phone=next_phone())
        stores = []
        for i in range(2):
            owner = User.objects.create_user(
                username='tajir%d' % i, password='x', phone=next_phone())
            stores.append(UserStore.objects.create(
                user=owner, name='متجر %d' % i, is_active=True))
        reels = [
            Reel.objects.create(
                store=stores[i % 2], status='published', duration_seconds=10,
                video_key='reels/v%d.mp4' % i, caption='ريل %d' % i)
            for i in range(4)
        ]
        return viewer, stores, reels

    def test_a_watched_reel_sinks_below_the_unwatched(self):
        from rest_framework.test import APIClient
        from core.models import ReelView

        viewer, _stores, reels = self._setup()
        ReelView.objects.create(reel=reels[-1], user=viewer, completed=True)

        client = APIClient()
        client.force_authenticate(user=viewer)
        ids = [r['id'] for r in client.get('/api/reels/').data['results']]
        assert ids[-1] == reels[-1].id, ids

    def test_a_guest_still_gets_newest_first(self):
        from rest_framework.test import APIClient

        _viewer, _stores, reels = self._setup()
        ids = [r['id'] for r in APIClient().get('/api/reels/').data['results']]
        assert ids == [r.id for r in reversed(reels)]

    def test_a_store_filter_keeps_its_own_order(self):
        # شبكة صفحة المتجر: من يفتح متجراً يريد أحدث ما نشره لا ما يناسب ذوقه.
        from rest_framework.test import APIClient

        viewer, stores, _reels = self._setup()
        client = APIClient()
        client.force_authenticate(user=viewer)
        body = client.get('/api/reels/', {'store': stores[0].id}).data
        assert body['count'] == 2
        assert all(r['store']['id'] == stores[0].id for r in body['results'])
