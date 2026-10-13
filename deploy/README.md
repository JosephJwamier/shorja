# النشر — الشورجة

النطاق: **shorjeh.com** · الـ API: **api.shorjeh.com** · الوسائط: **media.shorjeh.com**

كل أمر هنا يُنفَّذ على الخادم عبر SSH، من مجلد المشروع.

---

## 1. DNS على Cloudflare

| النوع | الاسم | القيمة | Proxy |
|---|---|---|---|
| A | `api` | IP الخادم | **DNS only** (رمادي) |
| A | `@` | IP الخادم | Proxied (برتقالي) |
| CNAME | `www` | `shorjeh.com` | Proxied |

> `api` يجب أن يبقى **DNS only** عند أول تشغيل: Caddy يحتاج وصولاً مباشراً على المنفذ 80 ليصدر شهادة Let's Encrypt. فعّل الوكيل (Proxy) بعد نجاح الشهادة إن أردت حماية DDoS.

`media` يُنشئه Cloudflare تلقائياً عند ربط النطاق المخصّص بالـ R2 bucket — لا تنشئه يدوياً.

---

## 2. تجهيز الخادم

```bash
# مستخدم غير root + مفتاح SSH، ثم:
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER   # سجّل خروجاً ودخولاً بعدها

sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443
sudo ufw enable
```

---

## 3. الكود والإعدادات

```bash
git clone <المستودع> balla && cd balla
cp deploy/.env.production.example .env
nano .env        # املأ كل الحقول الفارغة
chmod +x deploy/init-timezones.sh deploy/backup.sh
```

**مفتاح سرّي جديد** (لا تعد استعمال مفتاح التطوير):
```bash
python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits+'!@#$%^&*(-_=+)') for _ in range(50)))"
```

**مسار لوحة إدارة غير معروف:**
```bash
python3 -c "import secrets; print(secrets.token_hex(4)+'/')"
```

---

## 4. التشغيل

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.yml logs -f web
```

أول إقلاع يستغرق دقائق: بناء الصورة، تهيئة MySQL، **تحميل جداول المناطق الزمنية**، ثم `migrate` و`collectstatic`.

### تحقّق من أن الأساس سليم
```bash
# يجب أن يطبع "System check identified no issues"
docker compose -f deploy/docker-compose.yml exec web python manage.py check --database default
```
إن ظهر **core.E001** فجداول المناطق الزمنية لم تُحمَّل — شغّل:
```bash
docker compose -f deploy/docker-compose.yml exec db \
  sh -c 'mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root -p"$MYSQL_ROOT_PASSWORD" mysql'
docker compose -f deploy/docker-compose.yml restart web
```

### البيانات الأولية
```bash
C='docker compose -f deploy/docker-compose.yml exec web python manage.py'
$C createsuperuser
$C seed_categories
$C seed_legal_documents     # الخصوصية والشروط — يطلبهما المتجران
$C setup_celery_tasks
```

---

## 5. assetlinks.json (روابط المشاركة)

بعد توليد مفتاح التوقيع، خذ بصمة SHA-256:
```bash
keytool -list -v -keystore upload-keystore.jks -alias upload | grep SHA256
```
ثم على الخادم:
```bash
docker compose -f deploy/docker-compose.yml exec web \
  sh -c 'mkdir -p /app/staticfiles/wellknown && cat > /app/staticfiles/wellknown/assetlinks.json' <<'JSON'
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "com.balla.balla",
    "sha256_cert_fingerprints": ["ضع_البصمة_هنا"]
  }
}]
JSON
```

> **مهم:** أضف بصمتين — مفتاح الرفع **ومفتاح Play App Signing** (من Play Console ← App integrity). بدون الثانية تتعطل الروابط لكل من حمّل من المتجر.

تحقّق: `curl https://shorjeh.com/.well-known/assetlinks.json`

---

## 6. النسخ الاحتياطي

اتبع التعليمات في رأس `deploy/backup.sh` (تثبيت rclone، ربط R2، إضافة cron). ثم **اختبر الاسترجاع مرة واحدة** — نسخة لم تُجرَّب ليست نسخة.

---

## التشغيل اليومي

```bash
# السجلات
docker compose -f deploy/docker-compose.yml logs -f web worker beat

# نشر تحديث
git pull && docker compose -f deploy/docker-compose.yml up -d --build

# صدفة Django
docker compose -f deploy/docker-compose.yml exec web python manage.py shell
```

**نسخة واحدة من `beat` دائماً.** نسختان تعنيان إغلاق كل مزاد مرتين وتحرير كل دفعة مرتين.

---

## ما يبقى خارج هذا الملف

| | أين |
|---|---|
| مفتاح توقيع التطبيق | جهازك — `android/key.properties.example` |
| بصمات SHA في Firebase و Facebook | لوحتاهما |
| bucket و token و نطاق R2 | لوحة Cloudflare |
| بيانات القاصة الإنتاجية | عقدك معهم |
| Firebase Blaze للـ OTP | لوحة Firebase، مع حد إنفاق |
