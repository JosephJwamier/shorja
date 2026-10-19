# النشر — الشورجة

النطاق: **shorjeh.com** · الـ API: **api.shorjeh.com** · الوسائط: **media.shorjeh.com**

كل أمر هنا يُنفَّذ على الخادم عبر SSH، من مجلد المشروع.

---

## 1. DNS على Cloudflare

| النوع | الاسم | القيمة | Proxy |
|---|---|---|---|
| A | `api` | IP الخادم | **DNS only** (رمادي) |
| A | `live` | IP الخادم | **DNS only — دائماً** |
| A | `@` | IP الخادم | Proxied (برتقالي) |
| CNAME | `www` | `shorjeh.com` | Proxied |

> `live` ليس كـ`api`: الرمادي فيه حالة نهائية لا خطوة أولى. وكيل Cloudflare
> ينقل HTTP/HTTPS فقط، ووسائط البثّ على UDP/8000 والمرحّل على 3478 و5349 —
> فتمرّ المفاوضة على 443 وتنجح، وتُحجب الصورة. والأسوأ أن التطبيق يقرأ ذلك
> كـ«اكتملت المصافحة ولا صورة» فيُنزل كل مشاهد إلى HLS صامتاً، وتبقى تظنّ
> WebRTC يعمل.

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

## البثّ على نفس الخادم

البثّ يشارك هذا الخادم بدل صندوق منفصل. حزمة `infra/live/` ما زالت موجودة
للفصل لاحقاً، لكن `srs` و`coturn` معرّفان الآن في
`deploy/docker-compose.yml` نفسه — فهو الملف الذي يُشغَّل، ولا تعريف ثانياً
يُنسى تحديثه.

### لا تُشغّل `infra/live/bootstrap.sh` هنا

أوّل ما يفعله `ufw --force reset`. على صندوق فارغ هذا تهيئة؛ على هذا الخادم
هو مسح لقواعد جداره النارية ثم إعادة بناء مجموعة لا تعرف شيئاً عن إعداداتك.
المنافذ تُفتح يدوياً بدلاً منه:

```bash
sudo ufw allow 8000/udp        comment 'srs webrtc'
sudo ufw allow 8000/tcp        comment 'srs webrtc tcp'
sudo ufw allow 3478/udp        comment 'turn'
sudo ufw allow 3478/tcp        comment 'turn'
sudo ufw allow 5349/tcp        comment 'turns'
sudo ufw allow 49160:49200/udp comment 'turn relay'
```

منفذ RTMP (1935) ليس في القائمة عن قصد: لا يحتاجه إلا من يبثّ من OBS على
حاسوب، وفتحه دائماً يعني مدخلاً إضافياً لا يستعمله أحد. افتحه وقت اختبار
الحزمة ثم أغلقه.

### ما يُضبط في `.env`

`PUBLIC_IP` و`SRS_HOOK_SECRET` و`LIVE_TOKEN_SECRET` و`TURN_SECRET`
(`openssl rand -hex 32` لكل سرّ)، و`127.0.0.1` في `ALLOWED_HOSTS`. بدون
الأخير يردّ Django 400 على كل خطّاف فيمنع SRS كل بثّ ومشاهدة — وهو فشل لا
يظهر في أي سجل إلا سجل SRS.

و`SRS_HOOK_SECRET` نفسه يصل حاوية Caddy أيضاً (`deploy/docker-compose.yml`)،
فهي من تسأل Django عن كل طلب HLS. فارغاً عندها يردّ Django 403 على كل قائمة
ومقطع، فيختفي الطريق البديل كلّياً بينما يبقى WebRTC سليماً — انظر
«التحقّق من حماية HLS» أدناه.

و`LIVE_TOKEN_TTL_SECONDS`: اضبطه على أطول بثٍّ تسمح به (`21600` لستّ ساعات)
لا على ساعة. المشاهد على HLS يستعمل رمزاً واحداً طول مشاهدته — المشغّل يعيد
طلب نفس الرابط حتى يتوقّف — فانقضاء الرمز في منتصف البثّ يعني رفضاً ولا
إعادة اتصال في التطبيق: شاشة ميّتة حتى يخرج من الجلسة ويعود. وطول العمر هذا
رخيص لأن عمر الرمز ليس حدّ الحماية على هذا الطريق: الحظر وحالة الجلسة
يُقرآن كل ثوانٍ (`HLS_DECISION_TTL_SECONDS`)، فرمزٌ طويل العمر يتوقّف عن
العمل بعد ثوانٍ من الحظر أو انتهاء الجلسة.

### ثم

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d
docker compose -f deploy/docker-compose.yml logs -f srs coturn
```

اختبر من OBS **قبل** التطبيق: ظهور `on_publish` يعود `code: 0` في سجلّ SRS
يعني أن السلسلة كلها سليمة.

### التحقّق من حماية HLS

خطّافات SRS تغطّي النشر والمشاهدة على WebRTC فقط؛ أما قوائم HLS ومقاطعها
فملفات يخدمها SRS بنفسه ولا خطّاف لها. فيسألُ Caddy عنها: `forward_auth` في
`handle /live/*` يحوّل كل طلب إلى `‎/api/live/hls-auth/‎` ولا يخدم الملف إلا
بردّ 2xx. هذا يعني أن خطأً في الإعداد لا يُسقط الصورة بل يُسقط الطريق
البديل وحده بصمت — ولمن شبكته تحجب WebRTC هو الطريق الوحيد. فتحقّق منه بعد
كل نشر:

```bash
docker compose -f deploy/docker-compose.yml exec caddy \
  caddy validate --config /etc/caddy/Caddyfile
```

**ويلزم أن يُنشر التطبيق مع هذا الخادم.** الرمز يسافر في ترويسة
`X-Live-Token` لا في الرابط، لأن القائمة تسمّي مقاطعها بأسماء نسبية ولا
مشغّل ينقل سلسلة استعلامها إليها. فبناءٌ قديم من التطبيق لا يرسل الترويسة
يُرَدّ عليه 403 على طريق HLS كلّه ويرى صاحبه «تعذّر تشغيل البث على هذه
الشبكة»، بينما WebRTC يعمل عنده كما هو.

ثم، وبثٌّ حيّ قائم، استخرج اسم البثّ ورمز مشاهدة لحساب حقيقي:

```bash
docker compose -f deploy/docker-compose.yml exec web python manage.py shell -c \
  "from core.models import LiveSession
from core.services.live_media.srs import make_stream_token
s = LiveSession.objects.filter(status='live').first()
print(s.stream_name, make_stream_token(s.stream_name, 'subscriber', 1800, user_id=USER_ID))"
```

(ضع رقم حساب المشاهد مكان `USER_ID`.) ثم بالقيمتين:

```bash
STREAM=live_...; TOKEN=...; LIVE=https://live.shorjeh.com
code() { curl -sS -o /dev/null -w '%{http_code}\n' "$@"; }

code -H "X-Live-Token: $TOKEN" "$LIVE/live/$STREAM.m3u8"   # 200 — كما يطلبها التطبيق
code "$LIVE/live/$STREAM.m3u8?token=$TOKEN"                # 200 — الاستعلام بديل للترويسة
code "$LIVE/live/$STREAM.m3u8"                             # 403 — من نُقل إليه الرابط
code -H "X-Live-Token: abc" "$LIVE/live/$STREAM.m3u8"      # 403 — رمز مُلفَّق
```

والمقاطع تُفحَص بنفس المقياس تماماً، لأن المشغّل يكرّر الترويسة في كل طلب
(ExoPlayer على مصنع مصدر البيانات، وAVFoundation على الأصل نفسه):

```bash
SEG=$(curl -sS -H "X-Live-Token: $TOKEN" "$LIVE/live/$STREAM.m3u8" | grep -m1 '\.ts$')
code -H "X-Live-Token: $TOKEN" "$LIVE/live/$SEG"           # 200
code "$LIVE/live/$SEG"                                     # 403 — بلا رمز لا شيء
```

السطر الأخير هو بيت القصيد: لا عنوانَ IP يُعتَدّ به هنا ولا كوكي. العنوان
كان خياراً مطروحاً ورُفض لأن أكثر خطوط الجوّال العراقية خلف CGNAT — ولهذا
coturn في الحزمة أصلاً — فالعنوان العام مشاهدون كثيرون لا مشاهد واحد، وكان
المحظور سيركب منحة جاره ويستنتج اسم المقطع التالي من تسلسله. والكوكي كان
سيُهمَل على أندرويد: `DefaultHttpDataSource` في ExoPlayer يقوم على
`HttpURLConnection` ولا يعرف الكوكيز بلا `CookieHandler`، ولا يثبّته
`video_player_android` ولا أندرويد.

وأخيراً ما يعني هذا كلّه: احظر هذا المشاهد من التطبيق، ثم أعد السطر الأول.
يصير `403` في غضون خمس ثوانٍ — عمر القرار المحفوظ
(`HLS_DECISION_TTL_SECONDS` في `core/live_hooks.py`)، تزيد عليها الثواني
التي خزنها المشغّل عنده. والحدّ لكل مشاهد على حدته، فلا يطيله من يشاركه
عنوانه. لا فورياً عن قصد: الفور كان يعني استعلام حظرٍ لكل مقطعٍ من كل
مشاهد، أي نحو مئة استعلام في الثانية عند مئة مشاهد.

### ما يكلّفه هذا الاشتراك

| | |
|---|---|
| **النطاق الترددي** | كل مشاهد يأخذ البثّ كاملاً من الخادم — لا توزيع بين المشاهدين. مئة مشاهد على 720p ≈ ‎150–250 Mbit/s خارجة، من نفس الأنبوب الذي يخدم الـAPI والصور. هذا هو السقف الحقيقي، لا المعالج. |
| **نطاق الأعطال** | اليوم بثٌّ سيّئ لا يؤذي السوق. بعد الاشتراك، تشبُّع النطاق أو امتلاء القرص يُسقط `api.shorjeh.com` معه — أي المحفظة والدردشة والمزادات. |
| **المرحّل على 443** | مُعطَّل: Caddy يحتجزه. يُستعاد بعنوان IP ثانٍ (انظر `infra/live/turnserver.conf`). حتى ذلك الحين يعمل المرحّل على 3478 و5349، وهو يكفي CGNAT لا الشبكات التي ترشّح بالمنفذ. |
| **التسجيل** | مطفأ (`SRS_DVR=off`). لا تُشغّله هنا قبل أن يرفع `on_dvr` الملفات ويحذفها — قرص ممتلئ يوقف MySQL. |

مقاطع HLS في الرام (`tmpfs`) لا على القرص، فلا تزاحم كتابات MySQL كل ثانيتين.

---

## ما يبقى خارج هذا الملف

| | أين |
|---|---|
| مفتاح توقيع التطبيق | جهازك — `android/key.properties.example` |
| بصمات SHA في Firebase و Facebook | لوحتاهما |
| bucket و token و نطاق R2 | لوحة Cloudflare |
| بيانات القاصة الإنتاجية | عقدك معهم |
| Firebase Blaze للـ OTP | لوحة Firebase، مع حد إنفاق |
