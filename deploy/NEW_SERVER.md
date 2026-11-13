# خادم جديد — من صندوق فارغ إلى سوق يعمل

`README.md` يشرح تنصيباً من الصفر. هذا الملف يشرح **نقلاً**: عندك خادم يعمل
وتريد غيره. الفرق ليس في التنصيب — `bootstrap.sh` يتكفّل به — بل في أن
أشياء تعيش على الخادم القديم وحده ولا يعرفها Git ولا النسخ الاحتياطي.

اقرأ القسم صفر قبل أن تشتري شيئاً. قد يغيّر خطّتك.

---

## 0. ما يعيش على الخادم القديم وحده

| | محفوظ أين | ينجو من ضياع الخادم؟ |
|---|---|---|
| قاعدة البيانات | `backup.sh` ← R2 كل ليلة | **نعم** |
| صور المنتجات والمتاجر (`media`) | `backup.sh` ← R2 كل ليلة | **نعم** |
| `.env` بأسراره | قرص الخادم فقط | **لا** |
| `~/secrets/firebase-credentials.json` | قرص الخادم + جهازك | من جهازك فقط |
| `assetlinks.json` | داخل حجم `static` | **لا** |
| شهادات Caddy | حجم `caddy_data` | لا — وتُعاد تلقائياً |
| الريلز | R2 | نعم |

> **الصور صارت داخل النسخة الليلية** (2026-11-12). `backup.sh` يرفعها
> مرآةً ملفاً بملف إلى `r2:shorjeh-backups/media/` — يُضاف إليها ولا يُحذف
> منها. فخادمٌ يُفقد الليلة لا تُفقد معه صورة، واسترجاعها أمر `rclone` واحد
> في الخطوة ٥ لا نقلٌ يدويّ للحجم. (وبقيت تُخدَم من القرص المحلي كما كانت:
> لماذا، في رأس `backup.sh`.)
>
> **وما زال بلا نسخة: `.env`.** فيه كل الأسرار — كلمة قاعدة البيانات،
> ومفاتيح R2، وسرّ TURN — ولا شيء يحفظه خارج قرص الخادم. ولا ينبغي أن
> يُرفع إلى دلو النسخ نفسه: توكنٌ يحرس النسخ لا يُخزَّن داخلها، وإلّا فتحَ
> من سرق الخادم كلَّ شيء مرّةً واحدة. فاحتفظ بنسخته عندك أنت — وهي أول ما
> تأخذه في الخطوة ٢.

ولا يُنقل تلقائياً أيضاً، وكلّه خارج الخادم:

- سجلات DNS في Cloudflare (الخطوة ٦).
- قائمة IP المسموحة عند **القاصة** إن كانت تقيّد الاتصال بعنوان — اسألهم
  قبل التبديل، وإلّا توقّفت المدفوعات بعد النقل بلا خطأ مفهوم.
- cron الخاص بالنسخ الاحتياطي و`rclone config` (الخطوة ٨).

---

## 1. قبل أن تلمس الخادم الجديد

**خفّض TTL في Cloudflare** إلى دقيقتين لسجلات `api` و`live` و`delivery`
و`@` و`www`، وانتظر عمرَ TTL القديم كاملاً. تفعل هذا أولاً لأن قيمة TTL
التي تُسرّع التبديل هي القيمة المنشورة **قبله** لا بعده.

واعلم أن **الشهادات لا تُصدَر قبل تبديل DNS**: يتحقّق Let's Encrypt بطلبٍ
على المنفذ 80 للنطاق نفسه، فلا سبيل لاختبار HTTPS على الخادم الجديد قبل
توجيه الاسم إليه. لذا يُختبر كل شيء محلياً أولاً (الخطوة ٥)، ثم يُبدَّل DNS،
ثم تُراقَب الشهادات. ولهذا أيضاً: **لا تُقلّب DNS ذهاباً وإياباً** — لكل
نطاق خمس شهادات متطابقة في الأسبوع عند Let's Encrypt، ونفادها يتركك بلا
HTTPS أياماً.

---

## 2. على الخادم القديم: خذ ما لا يُعاد بناؤه

أوقف الكتابة أولاً حتى لا تنسخ قاعدةً تتغيّر تحت يدك:

```bash
cd ~/balla
docker compose -f deploy/docker-compose.yml stop worker beat web
```

تفريغ القاعدة:

```bash
set -a; source .env; set +a
docker compose -f deploy/docker-compose.yml exec -T db \
  mysqldump -u root -p"${MYSQL_ROOT_PASSWORD}" \
  --single-transaction --routines --triggers --quick "${DB_NAME}" \
  | gzip -9 > /tmp/balla-final.sql.gz
```

الصور — لا تُنزَّل إلى جهازك: المرآة الليلية تحملها أصلاً. شغّل النسخة
مرّةً أخيرة هنا لتلحق بها صور اليوم:

```bash
bash deploy/backup.sh
```

يرفع تفريغاً جديداً إلى `daily/` والجديد من الصور إلى `media/`. ولأنه يقرأ
الحجم بـ`--volumes-from` من حاوية `web` لا من مسارٍ على القرص، يعمل
والحاويات متوقّفة كما أوقفتها قبل سطور.

و`assetlinks.json` إن كنت قد أنشأته:

```bash
docker compose -f deploy/docker-compose.yml run --rm --no-deps -T \
  -v /tmp:/out web cat /app/staticfiles/wellknown/assetlinks.json > /tmp/assetlinks.json
```

ثم أنزل الاثنين و`.env` إلى جهازك:

```bash
scp -i ~/.ssh/<مفتاحك> <مستخدم>@<IP القديم>:/tmp/balla-final.sql.gz .
scp -i ~/.ssh/<مفتاحك> <مستخدم>@<IP القديم>:/tmp/assetlinks.json .
scp -i ~/.ssh/<مفتاحك> <مستخدم>@<IP القديم>:~/balla/.env .
```

ومفتاح Firebase موجود أصلاً على جهازك في مجلد المشروع
(`firebase-credentials.json`، وهو في `.gitignore`)، فلا حاجة لتنزيله.

**أعد تشغيل القديم** بعدها (`up -d`) ليبقى يخدم حتى التبديل. القاعدة التي
نسختَها ستتأخّر عن الواقع بما يدخل بينهما — لذا يُعاد التفريغ مرة أخيرة في
الخطوة ٦، وهذه النسخة بروفة.

---

## 3. الخادم الجديد: التهيئة

ادخل **كـ root** بمفتاحك (Hetzner يضعه لك عند الإنشاء)، وأنجز كل هذا القسم
في هذه الجلسة الواحدة — لأن المستخدم الذي ينشئه السكربت يدخل مجموعة
`docker` ولا يدخل `sudo`، فلن يستطيع هو فتح منفذ ولا تعديل جدار.

```bash
apt-get update && apt-get install -y git
git clone https://github.com/JosephJwamier/shorja.git /tmp/shorja
bash /tmp/shorja/deploy/bootstrap.sh
```

يُثبّت دوكر، ويُفعّل الجدار على SSH و80 و443، ويُنشئ المستخدم `shorjeh`
وينسخ إليه مفتاحك.

**منافذ البثّ ليست فيه** — افتحها الآن وأنت ما زلت root، وإلّا اكتملت
المصافحة ولم تأتِ صورة:

```bash
ufw allow 8000/udp        comment 'srs webrtc'
ufw allow 8001/tcp        comment 'srs webrtc tcp'
ufw allow 3478/udp        comment 'turn'
ufw allow 3478/tcp        comment 'turn'
ufw allow 5349/tcp        comment 'turns'
ufw allow 49160:49200/udp comment 'turn relay'
```

> 8001 لا 8000 للـTCP: المنفذ 8000 يحمل daphne على الحلقة المحلية، ووضع
> مستمع SRS عليه أوقف البثّ (`0e5b3ea`, `972d842`).

ومنفذ RTMP (1935) يُفتح وقت الاختبار من OBS ثم يُغلق.

وبعد أن تتأكّد أن مفتاحك يعمل للمستخدم الجديد — لا قبل — أقفل الدخول
بكلمة المرور (انظر ذاكرة «Server SSH access & hardening»).

---

## 4. الكود والإعدادات

اخرج وادخل كـ `shorjeh` هذه المرة:

```bash
ssh shorjeh@<IP الجديد>
git clone https://github.com/JosephJwamier/shorja.git ~/balla && cd ~/balla
```

ارفع `.env` القديم كما هو، ثم **غيّر فيه سطراً واحداً**:

```
PUBLIC_IP=<IP الخادم الجديد>
```

هذا وحده لأن كل ما عداه معلّق بالأسماء لا بالعناوين. و`PUBLIC_IP` يدخل
حرفياً في مرشّحات ICE وفي `external-ip` عند coturn — وخطؤه لا يُفشل
المصافحة بل يُنجحها بلا صورة، وهو أصعب عطلٍ يُشخَّص.

راجع أن هذه موجودة، فالنقل يُنسيها: `127.0.0.1` ضمن `ALLOWED_HOSTS`،
و`SRS_HLS_ORIGIN=http://host.docker.internal:8080`، و`SRS_DVR=off`.

ثم **مفتاح Firebase**، من جهازك:

```bash
ssh shorjeh@<IP الجديد> 'mkdir -p -m 700 ~/secrets'
scp firebase-credentials.json shorjeh@<IP الجديد>:~/secrets/
ssh shorjeh@<IP الجديد> 'chmod 600 ~/secrets/firebase-credentials.json'
```

بدونه لا تصل الإشعارات إلى أي هاتف، ولا يعمل الدخول بـGoogle (يردّ 503).
وغيابه لا يُسقط شيئاً ظاهراً، ولذلك بقي الخادم الأول بلا إشعارات شهراً
كاملاً. لهذا يرفض `docker compose up` الآن البدء إن لم يجد الملف (انظر
`x-firebase-key` في `docker-compose.yml`). إن رأيت خطأ `bind source path
does not exist` فهذه الخطوة هي التي نقصت.

---

## 5. استرجع البيانات ثم شغّل — بهذا الترتيب

القاعدة أولاً ووحدها، لأن `web` يشغّل `migrate` عند إقلاعه وليس من مصلحتك
أن يسابق الاسترجاع:

```bash
docker compose -f deploy/docker-compose.yml up -d db
docker compose -f deploy/docker-compose.yml ps   # انتظر healthy
```

```bash
set -a; source .env; set +a
gunzip -c /tmp/balla-final.sql.gz | docker compose -f deploy/docker-compose.yml \
  exec -T db mysql -u root -p"${MYSQL_ROOT_PASSWORD}" "${DB_NAME}"
```

ثم البقية:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

ثم الصور — من المرآة في R2، لا من ملفٍّ نقلته بيدك.

يتطلّب هذا `rclone` مُعدّاً على الخادم الجديد، فنفّذ أول نقطة من **الخطوة ٨**
(تثبيت `rclone` و`rclone config`) قبل هذا الأمر:

```bash
docker run --rm \
  --volumes-from "$(docker compose -f deploy/docker-compose.yml ps -aq web | head -n1)" \
  -v "$(rclone config file | tail -n1):/config/rclone/rclone.conf:ro" \
  rclone/rclone:latest copy r2:shorjeh-backups/media/ /app/media
```

> لا `:ro` بعد `--volumes-from` هنا — وهو الفرق الوحيد عن أمر النسخ في
> `backup.sh`. المهمّة الليلية تركّب الحجم للقراءة فقط عمداً حتى لا تملك
> إتلاف ما تحميه؛ والاسترجاع وحده يكتب فيه، بيدٍ وبقرار.

ثم تأكّد أن العدد يطابق المصدر — لا أن الأمر «نجح» وحسب:

```bash
docker compose -f deploy/docker-compose.yml exec web sh -c 'find /app/media -type f | wc -l'
```

وتحقّق قبل أن تلمس DNS — كل هذا يعمل بلا شهادة:

```bash
docker compose -f deploy/docker-compose.yml exec web python manage.py check --database default
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/
docker compose -f deploy/docker-compose.yml exec caddy caddy validate --config /etc/caddy/Caddyfile
curl -s http://127.0.0.1:1985/api/v1/versions
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8000/api/live/srs/on_publish/?secret=wrong    # 403 سليم، 400 يعني ALLOWED_HOSTS
```

> ظهور `core.E001` يعني أن جداول المناطق الزمنية لم تُحمَّل — الأمر في
> `README.md § 4`. وهي تُحمَّل عند أول تهيئة للقاعدة فقط، فإن استرجعت فوق
> حجم قديم فتفقّدها.

وأعد `assetlinks.json` (الأمر في `README.md § 5`) — بدونه يفشل التحقّق من
روابط التطبيق بصمت: الروابط تفتح المتصفح بدل التطبيق ولا خطأ في أي سجل.

---

## 6. التبديل

1. على القديم: `docker compose -f deploy/docker-compose.yml stop worker beat web`
   — **أوقف `beat` قبل كل شيء.** نسختان منه تعنيان إغلاق كل مزاد مرتين
   وتحرير كل دفعة مرتين، وهذه تُصلَّح بالمال لا بالكود.
2. أعد تفريغ القاعدة من القديم واسترجعها في الجديد (أوامر الخطوتين ٢ و٥).
   تأخذ دقائق، وهي نافذة التوقّف الحقيقية.
3. الصور التي رُفعت بين النسختين: `bash deploy/backup.sh` على القديم مرّة
   أخرى، ثم أعد أمر الاسترجاع من الخطوة ٥ على الجديد. المرآة تنقل الجديد
   وحده، فهي ثوانٍ لا دقائق.
4. في Cloudflare: وجّه `api` و`live` و`delivery` و`@` و`www` إلى العنوان
   الجديد. و`api` و`live` و`delivery` **رمادية (DNS only)**؛ و`live` تبقى
   رمادية للأبد — الوكيل ينقل HTTP/HTTPS فقط، فتمرّ المفاوضة وتُحجب
   الصورة، ويقرأ التطبيق ذلك سقوطاً إلى HLS بلا شكوى.
5. راقب الشهادات تُصدَر: `docker compose -f deploy/docker-compose.yml logs -f caddy`

---

## 7. تحقّق بعد التبديل

```bash
for d in api live delivery; do curl -s -o /dev/null -w "$d %{http_code}\n" https://$d.shorjeh.com/; done
curl -s -o /dev/null -w "%{http_code}\n" https://shorjeh.com/.well-known/assetlinks.json
```

وأن Firebase يقبل المفتاح، لا أن الملف موجود فقط. مفتاحٌ أُلغي من لوحة
Firebase يُقرأ بلا خطأ ثم يُرفض عند أول إرسال:

```bash
docker compose -f deploy/docker-compose.yml exec -T worker python manage.py shell -c "
from core.firebase import get_firebase_app; from firebase_admin import messaging
get_firebase_app()
messaging.send(messaging.Message(topic='healthcheck'), dry_run=True); print('firebase ok')"
```

`dry_run` يمرّ بالمصادقة كاملة ولا يصل منه شيء إلى أي هاتف.

ثم بثّ حقيقي من هاتف: ابدأ جلسة وشاهدها من هاتف آخر على شبكة جوّال لا
على نفس الواي-فاي — فمسار المرحّل (coturn) هو ما لا يُختبر إلا هكذا، وهو
المسار الذي يسلكه أكثر مستخدميك.

وتحقّق من حماية HLS (`README.md § التحقّق من حماية HLS`): ثلاثة أسطر
`curl` تثبت أن القائمة بلا رمز تُرفض وبرمز صحيح تُقبل.

---

## 8. ما يبقى بعد أن يعمل كل شيء

- **النسخ الاحتياطي:** ثبّت `rclone`، أعد `rclone config`، وثبّت نسخة
  صورة `rclone` في `.env` (التعليمات الأربع في رأس `backup.sh`)، وأضف سطر
  `crontab`. بلا هذه الخطوة يعمل الخادم الجديد بلا نسخة أي ليلة — لا
  للقاعدة ولا للصور. و`SENTRY_DSN` في `.env` هو ما يُنبّهك إن توقّفت.
- **جرّب الاسترجاع مرة واحدة** — القاعدة على قاعدة اختبار، والصور بأن تطلب
  ملفاً واحداً من المرآة وتفتحه. نسخةٌ لم تُجرَّب ليست نسخة:

  ```bash
  rclone lsf --files-only -R r2:shorjeh-backups/media/ | head
  rclone copy r2:shorjeh-backups/media/item_images /tmp/probe --include '*.jpg' --max-transfer 1M
  ```
- **تحقّق أن الصور تُرفَع فعلاً بعد أول ليلة** — سطر `[backup] ok` في
  `/var/log/balla-backup.log` يطبع عدد الملفات، وصفرٌ هناك مع متجر عامل
  يعني أن الحجم لم يُركَّب:

  ```bash
  tail -1 /var/log/balla-backup.log   # ‎[backup] ok … (db 12M / media 3417 files)
  ```
- **أوقف الخادم القديم** بعد يوم أو يومين من استقرار كل شيء، لا قبل. واحتفظ
  بـ`.env` القديم عندك حتى تتأكّد.
- التطبيق لا يُعاد بناؤه: يقصد `api.shorjeh.com` بالاسم، وقد تبعه DNS.
