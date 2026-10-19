# البث المباشر على سيرفر خاص — SRS + coturn + Caddy

الحزمة التي يعمل عليها البث على صندوق Hetzner. **التطبيق والسيرفر عليها الآن** — لا يوجد مزوّد بث خارجي بعد الآن، فما لم يرتفع هذا الصندوق وتُضبط أسراره فلا بث أبداً (تردّ الواجهة 503).

```
هاتف التاجر ──WHIP──▶ SRS ──WHEP──▶ المشاهدون (< 1 ثانية)
                       ├──HLS──▶ احتياطي (~6 ثوانٍ) لمن يفشل WebRTC عنده
                       └──hooks──▶ Django (on_publish / on_play): هل يحق له؟
coturn: مرحّل لمن خلف CGNAT (زين/آسياسيل)   Caddy: TLS تلقائي + توجيه على 443
```

> **هذه الحزمة للصندوق المنفصل.** القرار الحالي تشغيل البثّ على خادم
> `api.shorjeh.com` نفسه، و`srs` و`coturn` معرّفان هناك في
> `deploy/docker-compose.yml` — فاقرأ قسم «البثّ على نفس الخادم» في
> `deploy/README.md` بدل ما يلي. يبقى هذا الملف لحالة الفصل، ويبقى
> `srs.conf` و`turnserver.conf` مشتركين بين الحالتين.
>
> **ولا تُشغّل `bootstrap.sh` على خادم التطبيق:** أوّل سطر فيه
> `ufw --force reset` يمسح جداره الناري.

## 1) الشراء
- Hetzner **Server Auction** — Falkenstein/Nuremberg.
- ≥ 6 أنوية، 32–64GB، NVMe، **منفذ 1 Gbit** (الترافيك غير محدود للمخصّص). ~€35–45/شهر.
- نظام: Ubuntu 24.04 عبر `installimage`.
- دومين وسجل `A`: `live.<domain>` → IP السيرفر. **قبل** أول تشغيل.

## 2) على السيرفر (مرة واحدة)
```bash
apt-get update && apt-get install -y git
git clone <backend repo> /opt/balla && cd /opt/balla/infra/live
cp .env.example .env && nano .env      # PUBLIC_IP, LIVE_DOMAIN, ACME_EMAIL, DJANGO_HOOK_BASE, الأسرار
bash bootstrap.sh                       # Docker + ufw + sysctl + تشغيل الحزمة
```
توليد الأسرار: `openssl rand -hex 32` لكل من `SRS_HOOK_SECRET` و`TURN_SECRET`.

تحقّق:
```bash
docker compose ps                        # srs / coturn / caddy = running
curl -I https://live.<domain>/           # 200 من Caddy بشهادة صالحة
curl -s http://127.0.0.1:1985/api/v1/versions
```

## 3) على Django (.env)
```
LIVE_MEDIA_PROVIDER=srs
SRS_PUBLIC_BASE_URL=https://live.<domain>
SRS_HOOK_SECRET=<نفس قيمة السيرفر>
LIVE_TOKEN_SECRET=<openssl rand -hex 32>
TURN_HOST=live.<domain>
TURN_SECRET=<نفس قيمة السيرفر>
```
ولا شيء أكثر: الكود كلّه موصول (`join_token` ← المزوّد، `live/srs/<event>/` ← الخطافات،
وسياسة المشاهدة في `_may_play`، والتطبيق على WHIP/WHEP). ما ينقص هو الصندوق وهذه القيم فقط.

## 4) اختبار قبل لمس التطبيق
- **نشر من لابتوب (OBS):** Server `rtmp://<IP>/live`، Stream key `test?token=<publish token>` — يُولَّد التوكن من Django shell:
  `make_stream_token('test', 'publisher', 3600, user_id=1)` (بعد إنشاء جلسة باسم `test`).
- **مشاهدة:** `https://live.<domain>/live/test.m3u8` في VLC، أو صفحة WHEP التجريبية داخل SRS.
- **راقب:** `docker compose logs -f srs` — يجب أن ترى `on_publish` يعود `code: 0`.

## المنافذ المفتوحة (bootstrap.sh)
| المنفذ | لماذا |
|---|---|
| 443/tcp+udp | Caddy (HTTPS) + coturn بديل لشبكات تحجب غير HTTPS |
| 80/tcp | شهادة Let's Encrypt |
| 8000/udp+tcp | وسائط WebRTC لـSRS |
| 1935/tcp | RTMP (OBS) |
| 3478, 5349, 49160–49200/udp | coturn |

## ما تحتاجه لاحقاً (ليس الآن)
- رفع تسجيلات DVR إلى S3 (`on_dvr` يسجّل المسار فقط).
- مراقبة: `docker compose logs`، وصفحة `/api/v1/streams` القراءة فقط عبر Caddy.
- نسخ احتياطي للـ`.env` فقط — كل شيء آخر قابل لإعادة البناء.
