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

STAMP=$(date +%Y%m%d-%H%M%S)
FILE="/tmp/balla-${STAMP}.sql.gz"

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

rclone copy "${FILE}" r2:shorjeh-backups/daily/
rm -f "${FILE}"

# استبقِ 30 يوماً. الأقدم من ذلك يُحذف تلقائياً.
rclone delete --min-age 30d r2:shorjeh-backups/daily/

echo "[backup] ok ${STAMP} ($(numfmt --to=iec "${SIZE}"))"
