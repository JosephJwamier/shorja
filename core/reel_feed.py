"""ترتيب خلاصة الريلز — بما شاهده المستخدم وأعجبه وعلّق عليه.

كانت الخلاصة `-created_at` محضاً. ثلاث نتائج لذلك:

١. **ما شوهد يُعاد عرضه.** `ReelView` يُسجّل من شاهد ماذا منذ البداية،
   والخلاصة لا تقرأه — فمن يفتح الريلز يوميّاً يبدأ كل مرّة من الأعلى
   ويعيد ما رآه أمس.
٢. **متجر واحد يبتلع الخلاصة.** ينشر عشرة ريلزات فتصير العشرة أوائل ما
   يراه الجميع.
٣. **ولا شيء يخصّ المشاهد.** متابَعاته واهتماماته لا تغيّر شيئاً.

وهذا الملفّ يُرتّب القائمة بدل ذلك. مبدؤه أن **ما لم يُشاهَد يسبق دائماً**
— فلا ترتيبَ ذكيّاً يُقدّم ريلاً رآه المستخدم على آخر لم يره — وما بعد ذلك
درجات.

الدوالّ هنا خالصة (تأخذ بيانات وتُرجع ترتيباً) كي تُختبر بلا HTTP ولا
قاعدة بيانات: الترتيب منطقٌ يستحقّ اختباراً مباشراً، لا أن يُستدلّ عليه من
شكل استجابة.
"""

# ما لم يُشاهَد يسبق كل شيء آخر. الرقم كبير عمداً: لا مجموع درجاتٍ يبلغه،
# فلا يُزاح ريل جديد لأجل آخر قديم مهما كانت صلته بالمشاهد.
UNSEEN_BONUS = 1000

# متجر يتابعه: أقوى إشارة صريحة يملكها المستخدم.
FOLLOWED_STORE = 60

# متجر تفاعل مع ريلزاته من قبل (إعجاب أو تعليق أو إكمال مشاهدة) — إشارة
# سلوكية لا تصريحية، فأقلّ من المتابعة وأعلى من لا شيء.
ENGAGED_STORE = 35

# التصنيف الذي يخصّ المنتج الموسوم في الريل، إن كان ضمن اهتمامات المشاهد.
# يتناقص برتبة الاهتمام: الأول أقوى من الخامس.
TOP_CATEGORY = 30

# التفاعل العام — سقفه منخفض عمداً. رفعُه يجعل الخلاصة تُعيد المشهور
# نفسه للجميع، وهو عكس المطلوب من سوق فيه متاجر صغيرة جديدة.
MAX_POPULARITY = 15


def score_reel(
    reel_id, store_id, category_id, likes_count,
    *, seen_ids, followed_store_ids, engaged_store_ids, category_rank,
):
    """درجة ريل واحد لمشاهد واحد."""
    score = 0

    if reel_id not in seen_ids:
        score += UNSEEN_BONUS
    if store_id in followed_store_ids:
        score += FOLLOWED_STORE
    elif store_id in engaged_store_ids:
        # elif لا if: المتابعة تتضمّن الاهتمام، وجمعهما يُضاعف إشارةً واحدة.
        score += ENGAGED_STORE

    rank = category_rank.get(category_id)
    if rank is not None:
        score += max(TOP_CATEGORY - rank * 5, 5)

    score += min(likes_count or 0, MAX_POPULARITY)
    return score


def spread_stores(rows, *, max_gap=4):
    """يمنع ظهور متجرين متتاليين من نفس المتجر.

    [rows] مرتّبة بالدرجة تنازليّاً، وكل صفّ `(reel_id, store_id)`.

    مرور جشع: نأخذ الأعلى درجةً الذي لا يُطابق متجر سابقه؛ فإن كان كل
    المتبقّي من ذلك المتجر أخذنا الأعلى على أي حال — فالتنويع تحسينٌ لا
    شرط، وإسقاط ريل لأجله خسارة صافية.

    [max_gap] يحدّ كم ننظر إلى الأمام بحثاً عن متجر مختلف. بلا حدّ يصير
    المرور تربيعيّاً على قوائم طويلة، وبما أن الهدف كسر التتابع المباشر
    فالنظر أربعة صفوف يكفي.
    """
    remaining = list(rows)
    out = []
    last_store = None

    while remaining:
        pick = 0
        if last_store is not None:
            for i, (_reel_id, store_id) in enumerate(remaining[:max_gap]):
                if store_id != last_store:
                    pick = i
                    break
        reel_id, store_id = remaining.pop(pick)
        out.append(reel_id)
        last_store = store_id

    return out


def rank_reels(
    candidates, *, seen_ids, followed_store_ids, engaged_store_ids,
    category_rank,
):
    """ترتيب نهائي لمعرّفات الريلز.

    [candidates] تسلسل من `(reel_id, store_id, category_id, likes_count)`
    مرتّب بالأحدث أولاً — فترتيب النشر هو ما يفضّ تعادل الدرجات، وهو
    السلوك القديم حيث لا تمييز.
    """
    unseen, seen = [], []
    for index, (reel_id, store_id, category_id, likes) in enumerate(candidates):
        score = score_reel(
            reel_id, store_id, category_id, likes,
            seen_ids=seen_ids,
            followed_store_ids=followed_store_ids,
            engaged_store_ids=engaged_store_ids,
            category_rank=category_rank,
        )
        # index يحفظ ترتيب الأحدث عند التعادل.
        row = (-score, index, reel_id, store_id)
        (seen if reel_id in seen_ids else unseen).append(row)

    # التنويع يجري **داخل** كل مجموعة لا عبرهما.
    #
    # تطبيقه على القائمة كاملةً كان ينقض القاعدة الأولى: إن كان تالي
    # غير المشاهَد من نفس متجر سابقه، التقط المرورُ الجشع ريلاً مشاهَداً
    # من متجر آخر وقدّمه عليه. والدرجة الكبيرة لغير المشاهَد لا تحمي من
    # ذلك، لأن التنويع يُعيد الترتيب بعد انتهاء الدرجات من عملها.
    def ordered(rows):
        rows.sort()
        return spread_stores([(rid, sid) for _s, _i, rid, sid in rows])

    return ordered(unseen) + ordered(seen)
