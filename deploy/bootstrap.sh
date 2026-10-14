#!/bin/bash
# تهيئة خادم Ubuntu 24.04 نظيف لتشغيل الشورجة.
#
# يُشغَّل مرة واحدة كـ root على خادم جديد:
#   bash bootstrap.sh
#
# ما يفعله: تحديث النظام، دوكر، جدار ناري، ومستخدم تشغيل غير root.
# ما لا يفعله عمداً: تعطيل الدخول بكلمة المرور — تلك خطوة لاحقة بعد
# التأكد من أن مفتاح SSH يعمل، وإلا أقفلت على نفسك الباب.
set -euo pipefail

APP_USER="${APP_USER:-shorjeh}"

echo "==> 1/5 تحديث النظام"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

echo "==> 2/5 أدوات أساسية + دوكر"
apt-get install -y -qq \
    docker.io docker-compose-plugin \
    git curl ufw fail2ban unattended-upgrades
systemctl enable --now docker
systemctl enable --now fail2ban

echo "==> 3/5 الجدار الناري"
# SSH أولاً ودائماً: تفعيل ufw قبل السماح به يقطع جلستك الحالية.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> 4/5 مستخدم التشغيل: ${APP_USER}"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "${APP_USER}"
fi
usermod -aG docker "${APP_USER}"
# انسخ مفاتيح SSH الخاصة بـ root ليتمكن المستخدم الجديد من الدخول بها.
if [ -f /root/.ssh/authorized_keys ]; then
    install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.ssh"
    install -m 600 -o "${APP_USER}" -g "${APP_USER}" \
        /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
fi

echo "==> 5/5 تحديثات الأمان التلقائية"
# ترقيع ثغرات النظام بلا تدخل. لا يعيد التشغيل تلقائياً.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo
echo "================================================================"
echo "تمت التهيئة."
echo
echo "نسخة دوكر : $(docker --version)"
echo "المستخدم  : ${APP_USER} (ضمن مجموعة docker)"
echo "الجدار    : $(ufw status | head -1)"
echo
echo "الخطوة التالية — ادخل كـ ${APP_USER} واستنسخ المشروع:"
echo "  ssh ${APP_USER}@\$(curl -s ifconfig.me)"
echo "================================================================"
