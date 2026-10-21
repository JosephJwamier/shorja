#!/bin/bash
# نسخة احتياطية يومية لقاعدة البيانات إلى Cloudflare R2.
#
# لماذا R2 لا القرص المحلي: نسخة على نفس الخادم لا تحميك من فقدان الخادم،
# ولا من فساد قرص. وR2 بلا رسوم خروج، فالاسترجاع لا يكلّف شيئاً.
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
#   4) crontab -e  وأضف:
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
    curl -sS -m 10 -X POST "https://${host}/api/${project}/store/"         -H "Content-Type: application/json"         -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=${key}, sentry_client=balla-backup/1.0"         -d "{\"message\":\"${message}\",\"level\":\"error\",\"platform\":\"other\",\"server_name\":\"$(hostname)\",\"tags\":{\"job\":\"db-backup\"},\"environment\":\"${SENTRY_ENVIRONMENT:-production}\"}"         >/dev/null 2>&1 || true
}

# EXIT لا ERR: الخروج الصريح عند ملفّ أصغر من الحدّ الأدنى لا يُطلق ERR،
# وهو أحد أهمّ حالات الفشل التي نريد أن نُنبَّه لها.
STEP="بدء التشغيل"
on_exit() {
    local rc=$?
    [ "${rc}" -eq 0 ] && return 0
    echo "[backup] FAILED during: ${STEP} (exit ${rc})" >&2
    sentry_alert "فشل النسخ الاحتياطي لقاعدة البيانات — ${STEP} (رمز ${rc})"
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

echo "[backup] ok ${STAMP} ($(numfmt --to=iec "${SIZE}"))"
