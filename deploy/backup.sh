#!/bin/bash
# نسخة احتياطية يومية إلى Cloudflare R2: قاعدة البيانات، والصور المرفوعة.
#
# لماذا R2 لا القرص المحلي: نسخة على نفس الخادم لا تحميك من فقدان الخادم،
# ولا من فساد قرص. وR2 بلا رسوم خروج، فالاسترجاع لا يكلّف شيئاً.
#
# ما يُرفع — ولكلٍّ قاعدةٌ مختلفة، لأن طبيعتهما مختلفة:
#
#   daily/   تفريغ MySQL مضغوط: ملفّ جديد كل ليلة، يُحذف بعد ثلاثين يوماً.
#            القاعدة تتبدّل كلها كل يوم، فالقيمة في «لقطة ليلةٍ بعينها».
#
#   media/   الصور المرفوعة: مرآة ملفاً بملف، يُضاف إليها ولا يُحذف منها.
#            الصورة تُكتب مرّة ولا تُعدَّل، فلا معنى للقطات متعاقبة منها —
#            المطلوب أن توجد، لا أن تُؤرَّخ.
#
# ولماذا مرآةٌ لا أرشيف (tar كل ليلة): الأرشيف يُعيد نسخ كل صورة من جديد
# كل ليلة فينمو بلا سقف، واسترجاعه فكٌّ يدويّ للكلّ حتى لو ضاعت صورة
# واحدة. المرآة ترفع الجديد وحده، واسترجاعها أمر rclone واحد — وهو نفسه
# ما يُلغي النقل اليدوي للحجم عند تغيير الخادم (NEW_SERVER.md § ٢).
#
# وcopy لا sync بقصد: sync يحذف من الوجهة ما غاب عن المصدر، فحذفٌ خاطئ
# على الخادم يمحو النسخة الوحيدة في الليلة نفسها. وcopy لا يحذف شيئاً
# أبداً. ثمنها أن ملفّاً حُذف عمداً يبقى في الدلو — ثمنٌ بالبايتات، في
# مقابل حمايةٍ من خطأٍ لا رجعة فيه.
#
# ولماذا بقيت الصور تُخدَم من القرص ولم تنتقل إلى R2 تخزيناً افتراضياً
# (STORAGES['default'])، وهو البديل الآخر: ذلك يغيّر رابط كل صورة دفعةً
# واحدة — و/media/* يُخدَم من نطاقين (api.shorjeh.com، وshorjeh.com لأجل
# og:image في صفحات المشاركة)، والتطبيق المنشور يعرض ما تردّه الواجهة.
# خطأٌ هناك يُسقط كل صورة في السوق في اللحظة نفسها، ويحتاج قبله نقل كل
# ملفّ قائم إلى الدلو. والمرآة تشتري الهدف ذاته — «خادمٌ يُفقد الليلة لا
# تُفقد معه صورة» — دون أن تمسّ مسار الخدمة بشيء.
#
# التركيب على الخادم:
#   1) ثبّت rclone:  curl https://rclone.org/install.sh | sudo bash
#   2) rclone config  -> نوع s3، مزوّد Cloudflare، سمِّ الوجهة r2.
#      استعمل توكناً خاصاً بالنسخ الاحتياطي مقيّداً بـ shorjeh-backups —
#      لا مفتاح الريلز: خادم مخترق يجب ألّا يستطيع حذف نسخك.
#      ثم ألزِم rclone بعدم محاولة إنشاء الـ bucket:
#          rclone config update r2 no_check_bucket true
#      بدونها يستدعي CreateBucket قبل كل رفع ويفشل بـ 403، لأن توكناً
#      مقيّداً بـ bucket واحد لا يملك صلاحيات إدارية على الحساب.
#   3) أنشئ bucket اسمه shorjeh-backups في R2
#   4) الصور داخل حجم docker، و/var/lib/docker لا يدخله إلا root — ومستخدم
#      النشر في مجموعة docker لا في sudo. فتُقرأ عبر حاوية rclone تستعير
#      حجوم حاوية web (--volumes-from)، فتعمل بأي مستخدم يستطيع تشغيل هذا
#      السكربت أصلاً. اسحب الصورة ثم ثبّت نسختها في .env:
#          docker run --rm rclone/rclone:latest version
#          # ثم ضع في .env النسخة التي طبعها، مثلاً:
#          #   BACKUP_RCLONE_IMAGE=rclone/rclone:1.69.1
#      وبلا تثبيت تعمل المهمّة بـlatest — أي أنها تبدّل أداتها تحتك ليلةً
#      ما بلا أن تخبرك.
#   5) crontab -e  وأضف:
#        15 2 * * * /app/deploy/backup.sh >> /var/log/balla-backup.log 2>&1
#      (2:15 ليلاً بتوقيت بغداد — بعد مهمة احتساب درجات الثقة بساعة)
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

# ---------------------------------------------------------------------------
# التنبيه عند الفشل
# ---------------------------------------------------------------------------
# المهمّة فشلت ستّ ليالٍ متتالية (بتّ تنفيذ ضائع) والسجلّ يحمل الفشل، ولم
# يقرأه أحد. نسخةٌ احتياطية لا يُعلَم بتوقّفها ليست حماية بل وهم حماية —
# لأن اكتشافها يقع يوم الحاجة إليها بالضبط.
#
# والتنبيه بـcurl مباشرةً لا عبر حاوية Django: أرجح أسباب الفشل أن docker
# أو قاعدة البيانات معطّلة، وعندها يفشل أي تنبيه يمرّ بهما أيضاً. الأداة
# التي تُبلّغ عن العطب يجب ألّا تتعطّل به.
#
# تجربة التنبيه بلا انتظار عطب حقيقي:
#     BACKUP_SELFTEST=1 bash deploy/backup.sh
#
# ووسم job صار backup بعد أن صارت المهمّة ترفع الصور أيضاً (كان
# db-backup)، وpart يفرّق db عن media. فإن كانت لك في Sentry قاعدة تنبيهٍ
# تُرشّح على job، حدّثها ثم أعد الاختبار أعلاه — قاعدةٌ تُرشّح على اسمٍ لم
# يعد يُرسَل تصمت بالضبط حين تحتاجها.
sentry_alert() {
    local message="$1"
    [ -n "${SENTRY_DSN:-}" ] || return 0

    # https://KEY@HOST/PROJECT_ID
    local rest="${SENTRY_DSN#*://}"
    local key="${rest%%@*}"; key="${key%%:*}"   # صيغ قديمة: key:secret@
    local hostpath="${rest#*@}"
    local host="${hostpath%%/*}"
    local project="${hostpath##*/}"

    # `|| true` مرّتين: فشل التنبيه يجب ألّا يُغيّر رمز خروج المهمّة ولا
    # يُخفي سببها الحقيقي.
    curl -sS -m 10 -X POST "https://${host}/api/${project}/store/"         -H "Content-Type: application/json"         -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=${key}, sentry_client=balla-backup/1.0"         -d "{\"message\":\"${message}\",\"level\":\"error\",\"platform\":\"other\",\"server_name\":\"$(hostname)\",\"tags\":{\"job\":\"backup\",\"part\":\"${PART}\"},\"environment\":\"${SENTRY_ENVIRONMENT:-production}\"}"         >/dev/null 2>&1 || true
}

# EXIT لا ERR: الخروج الصريح عند ملفّ أصغر من الحدّ الأدنى لا يُطلق ERR،
# وهو أحد أهمّ حالات الفشل التي نريد أن نُنبَّه لها.
STEP="بدء التشغيل"
# أيّ شطرٍ من النسخة فشل — يصل Sentry وسماً، فلا يُقرأ العطب من النصّ وحده.
PART="db"
on_exit() {
    local rc=$?
    [ "${rc}" -eq 0 ] && return 0
    echo "[backup] FAILED during: ${STEP} (exit ${rc})" >&2
    sentry_alert "فشل النسخ الاحتياطي — ${STEP} (رمز ${rc})"
}
trap on_exit EXIT

if [ "${BACKUP_SELFTEST:-}" = "1" ]; then
    STEP="اختبار التنبيه"
    echo "[backup] self-test: sending a deliberate alert" >&2
    exit 90
fi

STAMP=$(date +%Y%m%d-%H%M%S)
FILE="/tmp/balla-${STAMP}.sql.gz"

STEP="تفريغ قاعدة البيانات"
# --single-transaction: نسخة متسقة بلا قفل الجداول، فالموقع يبقى يعمل.
# --routines --triggers: لا تترك المنطق المخزَّن خارج النسخة.
docker compose -f deploy/docker-compose.yml exec -T db     mysqldump -u root -p"${MYSQL_ROOT_PASSWORD}"     --single-transaction --routines --triggers --quick     "${DB_NAME}" | gzip -9 > "${FILE}"

SIZE=$(stat -c%s "${FILE}")
# الحد الأدنى يميّز "فشل صامت" عن "قاعدة صغيرة". mysqldump قد ينجح شكلياً
# ويُخرج ملفاً شبه فارغ عند خطأ مصادقة أو قاعدة غير موجودة؛ أما قاعدة
# حقيقية — ولو بلا معاملات — فمخططها وحده يتجاوز 20 كيلوبايت مضغوطاً
# (قاعدة الإطلاق الفارغة أنتجت 73 كيلوبايت). 20 كيلوبايت تمسك الفشل
# الحقيقي بلا رفض بداية مشروعة.
if [ "${SIZE}" -lt 20480 ]; then
    echo "[backup] FAILED: dump only ${SIZE} bytes" >&2
    rm -f "${FILE}"
    exit 1
fi

STEP="الرفع إلى R2"
rclone copy "${FILE}" r2:shorjeh-backups/daily/
rm -f "${FILE}"

STEP="حذف النسخ الأقدم من 30 يوماً"
# استبقِ 30 يوماً. الأقدم من ذلك يُحذف تلقائياً.
rclone delete --min-age 30d r2:shorjeh-backups/daily/

# ---------------------------------------------------------------------------
# مرآة الصور المرفوعة
# ---------------------------------------------------------------------------
PART="media"

STEP="إيجاد حاوية تحمل حجم الوسائط"
# لا نخمّن اسم الحجم: نسأل حاويةً تحمله أصلاً — وهي الحيلة نفسها التي
# يستعملها NEW_SERVER.md § ٢. وps -aq لا ps -q، فحاويةٌ متوقّفة تصلح تماماً
# لـ--volumes-from، ونشرٌ جارٍ وقت المهمّة يجب ألّا يُسقط نسخة الليلة.
# وworker بديلٌ عن web لأنه يركّب الحجم نفسه.
MEDIA_CID=""
for svc in web worker; do
    MEDIA_CID=$(docker compose -f deploy/docker-compose.yml ps -aq "${svc}" | head -n1)
    if [ -n "${MEDIA_CID}" ]; then break; fi
done
if [ -z "${MEDIA_CID}" ]; then
    echo "[backup] FAILED: no web/worker container to read the media volume from" >&2
    exit 1
fi

STEP="قراءة إعداد rclone"
# الوجهة نفسها والتوكن نفسه المقيّد بـshorjeh-backups — لا سرّ جديد يُدار.
RCLONE_CONF=$(rclone config file | tail -n1)
if [ ! -f "${RCLONE_CONF}" ]; then
    echo "[backup] FAILED: rclone config not found at ${RCLONE_CONF}" >&2
    exit 1
fi

# :ro — مهمّةٌ تحمي الصور لا ينبغي أن تملك تعديلها. والاسترجاع يُركّب الحجم
# للكتابة صراحةً، وهو أمرٌ يُكتب بيد (NEW_SERVER.md § ٥).
media_rclone() {
    docker run --rm \
        --volumes-from "${MEDIA_CID}:ro" \
        -v "${RCLONE_CONF}:/config/rclone/rclone.conf:ro" \
        "${BACKUP_RCLONE_IMAGE:-rclone/rclone:latest}" "$@"
}

STEP="إحصاء الصور"
SRC_FILES=$(media_rclone lsf --files-only -R /app/media | wc -l)
# `|| true` للوجهة وحدها: في أول تشغيل لا بادئة media/ في الدلو بعد، وغيابها
# ليس عطباً. وانقطاع R2 الحقيقي لا يمرّ من هنا — يُمسَك عند الرفع بعد سطور.
DST_FILES=$({ media_rclone lsf --files-only -R r2:shorjeh-backups/media/ || true; } | wc -l)

# يقابل الحدّ الأدنى لحجم التفريغ أعلاه: يميّز «قليلٌ بحق» من «فارغٌ صامت».
# ولا عتبة بالبايتات هنا بل مقارنة، لأن تنصيباً جديداً بلا صورةٍ واحدة
# شرعيّ تماماً — بينما مصدرٌ فارغ ومرآةٌ ممتلئة يعني أن الحجم لم يُركَّب
# (حاوية أُعيد إنشاؤها على حجمٍ آخر مثلاً). الأول يمرّ، والثاني لو مرّ
# لبقيت المرآة عند يومٍ قديم والمهمّة تُعلن النجاح كل ليلة.
STEP="التحقّق من أن الوسائط ليست فارغة صامتةً"
if [ "${SRC_FILES}" -eq 0 ] && [ "${DST_FILES}" -gt 0 ]; then
    echo "[backup] FAILED: media source is empty while the mirror holds ${DST_FILES} files" >&2
    exit 1
fi

STEP="رفع الصور إلى R2"
# copy لا sync، ولا حذف بالعمر: صورةٌ عمرها سنة لا تزال معروضة في المنصّة،
# فقاعدة الثلاثين يوماً أعلاه تخصّ التفريغ وحده.
media_rclone copy /app/media r2:shorjeh-backups/media/ \
    --transfers 8 --checkers 16 --stats-one-line --stats 1m

echo "[backup] ok ${STAMP} (db $(numfmt --to=iec "${SIZE}") / media ${SRC_FILES} files)"
