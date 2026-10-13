#!/bin/bash
# نسخة احتياطية يومية لقاعدة البيانات إلى Cloudflare R2.
#
# لماذا R2 لا القرص المحلي: نسخة على نفس الخادم لا تحميك من فقدان الخادم،
# ولا من فساد قرص. وR2 بلا رسوم خروج، فالاسترجاع لا يكلّف شيئاً.
#
# التركيب على الخادم:
#   1) ثبّت rclone:  curl https://rclone.org/install.sh | sudo bash
#   2) rclone config  -> نوع s3، مزوّد Cloudflare، نفس مفاتيح R2 في .env
#      سمِّ الوجهة r2
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
# نسخة أصغر من 100 كيلوبايت تعني فشلاً صامتاً (قاعدة فارغة أو خطأ مصادقة).
if [ "${SIZE}" -lt 102400 ]; then
    echo "[backup] FAILED: dump only ${SIZE} bytes" >&2
    rm -f "${FILE}"
    exit 1
fi

rclone copy "${FILE}" r2:shorjeh-backups/daily/
rm -f "${FILE}"

# استبقِ 30 يوماً. الأقدم من ذلك يُحذف تلقائياً.
rclone delete --min-age 30d r2:shorjeh-backups/daily/

echo "[backup] ok ${STAMP} ($(numfmt --to=iec "${SIZE}"))"
