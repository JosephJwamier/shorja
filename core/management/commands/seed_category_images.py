# -*- coding: utf-8 -*-
"""
seed_category_images
====================
يبحث في الإنترنت عن صور فوتوغرافية مجانية (مرخّصة للاستخدام التجاري) ويربط
صورة مناسبة بكل تصنيف في قاعدة البيانات (حقل Category.image).

يُشغَّل على جهازك حيث يعمل السيرفر (لأن الاتصال بقاعدة البيانات + الإنترنت
متوفران هناك):

    python manage.py seed_category_images                 # كل التصنيفات بلا صورة
    python manage.py seed_category_images --overwrite      # يعيد جلب الجميع
    python manage.py seed_category_images --only-parents    # التصنيفات الرئيسية فقط
    python manage.py seed_category_images --limit 5 --dry-run

مصادر الصور (تُجرَّب بالترتيب حتى ينجح واحد):
  1) Openverse  — بحث صور مرخّصة (CC / للاستخدام التجاري) بلا مفتاح API.
  2) LoremFlickr — صورة فوتوغرافية من Flickr مطابقة للكلمة المفتاحية.
  3) لوحة لونية مُولّدة محلياً — كحل أخير فقط إذا فشل الإنترنت (تُوسَم PLACEHOLDER).

لا يعتمد على أي مكتبة خارجية عدا Pillow (المثبّتة أصلاً مع المشروع).

يكتب تقريراً بمصادر وتراخيص الصور في: media/categories/_sources.csv
"""

import csv
import io
import json
import os
import random
import time
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from core.models import Category

try:
    from PIL import Image, ImageDraw
    # Pillow ≥9.1 يستخدم Image.Resampling؛ نوفّر بديلاً للإصدارات الأقدم.
    try:
        _LANCZOS = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover
        _LANCZOS = Image.LANCZOS
except ImportError:  # Pillow مطلوب أصلاً لحقل ImageField في Django
    Image = None
    _LANCZOS = None


# ---------------------------------------------------------------------------
# خريطة كلمات البحث لكل تصنيف (المفتاح = الاسم الإنجليزي كما في seed_categories)
# اخترتُ كلمات دقيقة لتُرجع صورة معبّرة عن التصنيف.
# أي تصنيف غير مذكور هنا يُبحَث باسمه الإنجليزي تلقائياً.
# ---------------------------------------------------------------------------
QUERIES = {
    # ---- التصنيفات الرئيسية ----
    "Electronics": "consumer electronics gadgets devices",
    "Vehicles": "cars vehicles transportation",
    "Fashion": "fashion clothing apparel boutique",
    "Home & Garden": "home interior furniture decor",
    "Beauty & Health": "cosmetics beauty skincare products",
    "Sports & Hobbies": "sports equipment fitness",
    "Kids & Babies": "baby kids colorful toys",
    "Books & Education": "books library education",
    "Real Estate": "modern house real estate building",
    "Art & Collectibles": "art painting gallery",
    "Animals & Pets": "pets dog cat cute",
    "Food & Groceries": "grocery fresh food vegetables market",

    # ---- إلكترونيات ----
    "Mobile Phones": "smartphone mobile phone",
    "Laptops & Computers": "laptop computer workspace",
    "Tablets": "tablet device screen",
    "TV & Audio": "television home theater speakers",
    "Cameras": "camera photography dslr lens",
    "Gaming": "video game console controller",
    "Electronic Accessories": "chargers cables electronic accessories",
    "Printers & Scanners": "office printer",
    "Networking": "wifi router networking device",

    # ---- مركبات ----
    "Cars": "car automobile modern",
    "Motorcycles": "motorcycle motorbike",
    "Trucks & Vans": "truck cargo van",
    "Car Parts": "car engine spare parts",
    "Car Accessories": "car interior accessories",
    "Boats": "boat yacht sea",
    "Bicycles": "bicycle bike",

    # ---- ملابس وأزياء ----
    "Men's Clothing": "men fashion suit clothing",
    "Women's Clothing": "women fashion dress boutique",
    "Kids' Clothing": "kids children clothing colorful",
    "Shoes": "shoes sneakers footwear",
    "Bags": "handbag leather bag fashion",
    "Watches": "wristwatch luxury watch",
    "Jewelry": "jewelry gold diamond ring",
    "Abayas & Thobes": "abaya traditional clothing elegant",
    "Sunglasses": "sunglasses eyewear",

    # ---- منزل وحديقة ----
    "Furniture": "modern furniture sofa living room",
    "Kitchen & Dining": "kitchen cookware dining",
    "Bedding & Pillows": "bedding pillows cozy bedroom",
    "Home Appliances": "home appliances kitchen",
    "Lighting": "lamp lighting interior design",
    "Garden & Outdoor": "garden plants outdoor green",
    "Tools & DIY": "tools hardware workshop",
    "Curtains & Decor": "curtains home decor interior",
    "Cleaning Supplies": "cleaning supplies household",

    # ---- جمال وصحة ----
    "Skincare": "skincare cream cosmetics",
    "Makeup": "makeup cosmetics palette",
    "Perfumes": "perfume bottle fragrance",
    "Hair Care": "hair care shampoo salon",
    "Medical Devices": "medical health device",
    "Vitamins & Supplements": "vitamins supplements pills health",
    "Personal Care": "personal care hygiene toiletries",

    # ---- رياضة وهوايات ----
    "Football": "soccer football ball green",
    "Gym Equipment": "gym fitness equipment dumbbell",
    "Outdoor & Camping": "camping tent outdoor nature",
    "Swimming": "swimming pool water",
    "Cycling": "cycling road bike sport",
    "Martial Arts": "boxing gloves martial arts",
    "Fishing": "fishing rod lake",
    "Board Games": "board game chess",

    # ---- أطفال ورضع ----
    "Toys": "children toys colorful",
    "Baby Gear": "baby stroller gear",
    "Kids Furniture": "kids nursery room furniture",
    "Educational Toys": "educational toys learning kids",
    "Kids Books": "children books reading",

    # ---- كتب وتعليم ----
    "School Books": "school books stack",
    "University Books": "university textbooks study",
    "Islamic Books": "islamic books quran",
    "Novels & Stories": "novel books reading cozy",
    "Stationery": "stationery pens notebook",
    "Online Courses": "online learning laptop study",

    # ---- عقارات ----
    "Apartments": "modern apartment building",
    "Houses": "house home exterior",
    "Land": "land field landscape",
    "Commercial": "commercial building storefront",
    "Farms": "farm agriculture field",

    # ---- فنون ومقتنيات ----
    "Artwork & Paintings": "painting art canvas colorful",
    "Antiques": "antique vintage collectible",
    "Coins & Stamps": "old coins stamps collection",
    "Handmade Crafts": "handmade craft artisan",

    # ---- حيوانات أليفة ----
    "Dogs": "dog puppy cute",
    "Cats": "cat kitten cute",
    "Birds": "colorful parrot bird",
    "Fish & Aquarium": "aquarium fish tank",
    "Pet Supplies": "pet supplies accessories",

    # ---- طعام وبقالة ----
    "Fresh Produce": "fresh vegetables fruits market",
    "Packaged Food": "packaged food grocery cans",
    "Dairy & Eggs": "dairy milk eggs",
    "Sweets & Snacks": "sweets candy snacks",
    "Beverages": "beverages drinks juice",
    "Spices & Herbs": "spices herbs colorful",
}

USER_AGENT = "BallaCategorySeeder/1.0 (+marketplace admin image seeding script)"
OUTPUT_SIZE = 800          # الصورة النهائية مربعة 800×800
JPEG_QUALITY = 85
MAX_BYTES = 20 * 1024 * 1024  # لا نقرأ أكثر من 20MB لأي صورة


# ---------------------------------------------------------------------------
# أدوات الشبكة (المكتبة القياسية فقط — لا حاجة لـ requests)
# ---------------------------------------------------------------------------
def _http_get(url, timeout=25):
    """يرجع (bytes, final_url) أو يرمي استثناء."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("image too large")
        return data, resp.geturl()


def _openverse_candidates(query):
    """يرجع قائمة نتائج Openverse (كل عنصر dict فيه url وبيانات الترخيص)."""
    params = urllib.parse.urlencode({
        "q": query,
        "page_size": 12,
        "license_type": "commercial",   # مسموح للاستخدام التجاري
        "mature": "false",
        "aspect_ratio": "wide,square",
    })
    url = "https://api.openverse.org/v1/images/?" + params
    data, _ = _http_get(url, timeout=25)
    payload = json.loads(data.decode("utf-8", "replace"))
    return payload.get("results", []) or []


# ---------------------------------------------------------------------------
# معالجة الصورة: تحويل لمربع 800×800 JPEG نظيف وموحّد الشكل
# ---------------------------------------------------------------------------
def _normalize(raw):
    """يأخذ bytes صورة خام ويرجع bytes JPEG مربعة، أو None إن لم تكن صورة صالحة."""
    if Image is None:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None

    # تجاهل الصور الصغيرة جداً (غالباً أيقونات/شعارات لا تصلح كخلفية)
    if min(img.size) < 300:
        return None

    img = img.convert("RGB")

    # قص من المنتصف إلى مربع
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((OUTPUT_SIZE, OUTPUT_SIZE), _LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _make_placeholder(name):
    """لوحة تدرّج لونية مُولّدة محلياً — حل أخير فقط."""
    if Image is None:
        return None
    seed = sum(ord(c) for c in name)
    rnd = random.Random(seed)
    c1 = (rnd.randint(40, 120), rnd.randint(60, 150), rnd.randint(90, 180))
    c2 = (min(c1[0] + 90, 255), min(c1[1] + 70, 255), min(c1[2] + 60, 255))
    img = Image.new("RGB", (OUTPUT_SIZE, OUTPUT_SIZE), c1)
    draw = ImageDraw.Draw(img)
    for y in range(OUTPUT_SIZE):
        t = y / OUTPUT_SIZE
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        draw.line([(0, y), (OUTPUT_SIZE, y)], fill=(r, g, b))
    # دائرة ناعمة للزينة
    draw.ellipse([OUTPUT_SIZE * 0.55, OUTPUT_SIZE * 0.55,
                  OUTPUT_SIZE * 1.15, OUTPUT_SIZE * 1.15],
                 fill=(c2[0], c2[1], c2[2]))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


class Command(BaseCommand):
    help = "يبحث عن صور فوتوغرافية مجانية ويربطها بكل تصنيف (Category.image)."

    def add_arguments(self, parser):
        parser.add_argument("--overwrite", action="store_true",
                            help="يعيد جلب الصور حتى للتصنيفات التي لها صورة.")
        parser.add_argument("--only-parents", action="store_true",
                            help="التصنيفات الرئيسية فقط (بلا أب).")
        parser.add_argument("--only-children", action="store_true",
                            help="التصنيفات الفرعية فقط.")
        parser.add_argument("--limit", type=int, default=0,
                            help="حدّ أقصى لعدد التصنيفات المعالَجة (0 = الكل).")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="فاصل زمني (ثوانٍ) بين كل تصنيف — لطيف على الخوادم.")
        parser.add_argument("--source", choices=["auto", "openverse", "loremflickr"],
                            default="auto", help="مصدر الصور (auto يجرّب الكل).")
        parser.add_argument("--dry-run", action="store_true",
                            help="يعرض ما سيفعله دون تنزيل أو حفظ.")

    # -- جلب صورة لتصنيف واحد، يرجع (jpeg_bytes, meta) --
    def _fetch_for(self, query, source):
        # 1) Openverse
        if source in ("auto", "openverse"):
            try:
                for r in _openverse_candidates(query):
                    img_url = r.get("url")
                    if not img_url:
                        continue
                    try:
                        raw, _ = _http_get(img_url, timeout=25)
                    except Exception:
                        continue
                    norm = _normalize(raw)
                    if norm:
                        meta = {
                            "source": "openverse",
                            "license": (r.get("license") or "") +
                                       (" " + str(r.get("license_version") or "")).strip(),
                            "creator": r.get("creator") or "",
                            "page": r.get("foreign_landing_url") or img_url,
                        }
                        return norm, meta
            except Exception as e:
                self.stderr.write(f"    (openverse فشل: {e})")

        # 2) LoremFlickr
        if source in ("auto", "loremflickr"):
            kw = urllib.parse.quote(",".join(query.split()[:4]))
            lf_url = f"https://loremflickr.com/1000/1000/{kw}"
            try:
                raw, final = _http_get(lf_url, timeout=25)
                norm = _normalize(raw)
                if norm:
                    return norm, {"source": "loremflickr", "license": "CC (Flickr)",
                                  "creator": "", "page": final}
            except Exception as e:
                self.stderr.write(f"    (loremflickr فشل: {e})")

        return None, None

    def handle(self, *args, **opts):
        if Image is None:
            self.stderr.write(self.style.ERROR(
                "Pillow غير متوفّر — ثبّته: pip install Pillow"))
            return

        qs = Category.objects.all().order_by("order", "id")
        if opts["only_parents"]:
            qs = qs.filter(parent__isnull=True)
        if opts["only_children"]:
            qs = qs.filter(parent__isnull=False)

        cats = list(qs)
        if not opts["overwrite"]:
            cats = [c for c in cats if not c.image]
        if opts["limit"]:
            cats = cats[:opts["limit"]]

        if not cats:
            self.stdout.write(self.style.WARNING(
                "لا يوجد تصنيفات لمعالجتها (كلها لديها صور؟ جرّب --overwrite)."))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"سيُعالَج {len(cats)} تصنيف — المصدر: {opts['source']}"
            + (" [تجريبي]" if opts["dry_run"] else "")))

        cats_dir = os.path.join(settings.MEDIA_ROOT, "categories")
        os.makedirs(cats_dir, exist_ok=True)
        report_rows = []

        ok = fell_back = placeholder = failed = 0

        for i, cat in enumerate(cats, 1):
            query = QUERIES.get(cat.name) or QUERIES.get(cat.name_ar) or (cat.name or cat.name_ar)
            label = f"[{i}/{len(cats)}] {cat.name_ar or cat.name}"

            if opts["dry_run"]:
                self.stdout.write(f"  {label:<32} ← بحث: \"{query}\"")
                continue

            jpeg, meta = self._fetch_for(query, opts["source"])

            if jpeg is None:
                # حل أخير: لوحة مُولّدة محلياً
                jpeg = _make_placeholder(cat.name_ar or cat.name)
                meta = {"source": "placeholder", "license": "-", "creator": "", "page": ""}
                if jpeg is None:
                    failed += 1
                    self.stdout.write(self.style.ERROR(f"  {label:<32} ✗ فشل"))
                    continue

            fname = f"{cat.slug or slugify(cat.name) or ('cat-%d' % cat.id)}.jpg"
            cat.image.save(fname, ContentFile(jpeg), save=True)

            src = meta["source"]
            if src == "openverse":
                ok += 1
                tag = self.style.SUCCESS("✓ Openverse")
            elif src == "loremflickr":
                fell_back += 1
                tag = self.style.SUCCESS("✓ Flickr")
            else:
                placeholder += 1
                tag = self.style.WARNING("~ PLACEHOLDER")
            self.stdout.write(f"  {label:<32} {tag}")

            report_rows.append([cat.id, cat.name, cat.name_ar, meta["source"],
                                meta["license"], meta["creator"], meta["page"], query])

            if opts["delay"] and i < len(cats):
                time.sleep(opts["delay"])

        # كتابة تقرير المصادر/التراخيص
        if report_rows:
            report_path = os.path.join(cats_dir, "_sources.csv")
            with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["id", "name", "name_ar", "source", "license",
                            "creator", "source_page", "query"])
                w.writerows(report_rows)
            self.stdout.write(f"\nتقرير المصادر: {report_path}")

        if not opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"\nتم: Openverse={ok}  Flickr={fell_back}  "
                f"Placeholder={placeholder}  فشل={failed}"))
            if placeholder:
                self.stdout.write(self.style.WARNING(
                    "بعض التصنيفات أخذت لوحة مؤقتة (تعذّر الإنترنت لها) — "
                    "أعد التشغيل لاحقاً مع --overwrite لجلب صور حقيقية لها."))
