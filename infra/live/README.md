# البث المباشر على سيرفر خاص — SRS + coturn + Caddy

الحزمة الجاهزة لتشغيل البث على صندوق Hetzner بدل Agora. **لا شيء منها يعمل الآن**؛ التطبيق ما زال على Agora حتى تقرّر التبديل.

```
هاتف التاجر ──WHIP──▶ SRS ──WHEP──▶ المشاهدون (< 1 ثانية)
                       ├──HLS──▶ احتياطي (~6 ثوانٍ) لمن يفشل WebRTC عنده
                       └──hooks──▶ Django (on_publish / on_play): هل يحق له؟
coturn: مرحّل لمن خلف CGNAT (زين/آسياسيل)   Caddy: TLS تلقائي + توجيه على 443
```

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
ثم خطوات التبديل الأربع المذكورة في `core/services/live_media/__init__.py`
(توجيه `core/live_hooks.py`، تحويل `join_token` إلى المزوّد، طبقة الوسائط في التطبيق، وسياسة `_may_play`).

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
