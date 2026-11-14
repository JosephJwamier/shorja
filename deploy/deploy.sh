#!/bin/bash
# نشر تحديث على الخادم بأمر واحد لا يموت إذا انقطع SSH.
#
# الخادم بمعالج واحد (nproc = 1)، وبناء الصورة يثقله حتى ينقطع SSH أحياناً
# — وكل أمر بدأه ذلك الاتصال يموت معه، فيبقى النشر نصف منجز: كود جديد
# مسحوب وحاوية قديمة تعمل. لذلك يعيد السكربت تشغيل نفسه منفصلاً عن الطرفية
# (setsid + nohup) ويكتب كل شيء في سجل: الانقطاع يُغلق شاشتك فقط، والنشر
# يكمل.
#
#   sudo bash deploy/deploy.sh              # ينشر ويعرض السجل حتى ينتهي
#   tail -f /home/shorjeh/deploy.log        # بعد انقطاع: تابع من حيث وصل
#
# الخطوات: git pull --ff-only بصفة مالك المستودع، ثم بناء web وworker وbeat
# وتشغيلها دون المساس بقاعدة البيانات (--no-deps) — وweb يطبّق الترحيلات
# عند بدئه — ثم إعادة إنشاء caddy إن تغيّر Caddyfile: ربط ملفٍّ مفرد داخل
# الحاوية لا يرى ما سحبه git، و«reload» يعيد قراءة الملف القديم.
#
# بوّابة التوصيل (delivery_portal/) ليست في git، فلا ينشرها هذا السكربت.
set -euo pipefail

REPO=/home/shorjeh/balla
OWNER=shorjeh
LOG=/home/shorjeh/deploy.log

if [ "${DEPLOY_DETACHED:-}" != 1 ]; then
    [ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
    DEPLOY_DETACHED=1 setsid nohup bash "$(readlink -f "$0")" >"$LOG" 2>&1 </dev/null &
    pid=$!
    echo "النشر يعمل منفصلاً (PID $pid). السجل: $LOG"
    echo "Ctrl+C يوقف المتابعة لا النشر."
    tail -n +1 -f "$LOG" --pid="$pid"
    exit 0
fi

as_owner() { sudo -u "$OWNER" "$@"; }
compose() { docker compose -f deploy/docker-compose.yml --env-file .env "$@"; }

echo "=== deploy started $(date -u '+%F %T') UTC ==="
cd "$REPO"

before=$(as_owner git rev-parse HEAD)
as_owner git pull --ff-only
after=$(as_owner git rev-parse HEAD)
echo "commit: ${before:0:7} -> ${after:0:7}"

compose up -d --build --no-deps web worker beat

if ! as_owner git diff --quiet "$before" "$after" -- deploy/Caddyfile; then
    echo "Caddyfile changed: recreating caddy"
    compose up -d --no-deps --force-recreate caddy
fi

compose ps
echo "=== deploy finished $(date -u '+%F %T') UTC ==="
