from django.core.management.base import BaseCommand
from django.utils.text import slugify
from core.models import Category


CATEGORIES = [
    {
        "name": "Electronics",
        "name_ar": "إلكترونيات",
        "icon": "📱",
        "order": 1,
        "children": [
            {"name": "Mobile Phones",        "name_ar": "هواتف ذكية",           "icon": "📱", "order": 1},
            {"name": "Laptops & Computers",  "name_ar": "لابتوب وحاسوب",        "icon": "💻", "order": 2},
            {"name": "Tablets",              "name_ar": "أجهزة لوحية",           "icon": "📟", "order": 3},
            {"name": "TV & Audio",           "name_ar": "تلفزيون وصوتيات",      "icon": "📺", "order": 4},
            {"name": "Cameras",              "name_ar": "كاميرات",               "icon": "📷", "order": 5},
            {"name": "Gaming",               "name_ar": "ألعاب فيديو",           "icon": "🎮", "order": 6},
            {"name": "Electronic Accessories","name_ar": "ملحقات إلكترونية",    "icon": "🔌", "order": 7},
            {"name": "Printers & Scanners",  "name_ar": "طابعات وماسحات ضوئية", "icon": "🖨️", "order": 8},
            {"name": "Networking",           "name_ar": "شبكات وإنترنت",        "icon": "📡", "order": 9},
        ],
    },
    {
        "name": "Vehicles",
        "name_ar": "مركبات",
        "icon": "🚗",
        "order": 2,
        "children": [
            {"name": "Cars",             "name_ar": "سيارات",          "icon": "🚗", "order": 1},
            {"name": "Motorcycles",      "name_ar": "دراجات نارية",    "icon": "🏍️", "order": 2},
            {"name": "Trucks & Vans",    "name_ar": "شاحنات وفانات",   "icon": "🚚", "order": 3},
            {"name": "Car Parts",        "name_ar": "قطع غيار",        "icon": "🔧", "order": 4},
            {"name": "Car Accessories",  "name_ar": "إكسسوارات سيارات","icon": "🪄", "order": 5},
            {"name": "Boats",            "name_ar": "قوارب",           "icon": "⛵", "order": 6},
            {"name": "Bicycles",         "name_ar": "دراجات هوائية",   "icon": "🚴", "order": 7},
        ],
    },
    {
        "name": "Fashion",
        "name_ar": "ملابس وأزياء",
        "icon": "👗",
        "order": 3,
        "children": [
            {"name": "Men's Clothing",   "name_ar": "ملابس رجالية",     "icon": "👔", "order": 1},
            {"name": "Women's Clothing", "name_ar": "ملابس نسائية",     "icon": "👗", "order": 2},
            {"name": "Kids' Clothing",   "name_ar": "ملابس أطفال",      "icon": "🧒", "order": 3},
            {"name": "Shoes",            "name_ar": "أحذية",            "icon": "👟", "order": 4},
            {"name": "Bags",             "name_ar": "حقائب",            "icon": "👜", "order": 5},
            {"name": "Watches",          "name_ar": "ساعات",            "icon": "⌚", "order": 6},
            {"name": "Jewelry",          "name_ar": "مجوهرات",          "icon": "💍", "order": 7},
            {"name": "Abayas & Thobes",  "name_ar": "عبايات وأثواب",   "icon": "🧕", "order": 8},
            {"name": "Sunglasses",       "name_ar": "نظارات شمسية",     "icon": "🕶️", "order": 9},
        ],
    },
    {
        "name": "Home & Garden",
        "name_ar": "منزل وحديقة",
        "icon": "🏠",
        "order": 4,
        "children": [
            {"name": "Furniture",            "name_ar": "أثاث",                  "icon": "🛋️", "order": 1},
            {"name": "Kitchen & Dining",     "name_ar": "مطبخ وأدوات طعام",     "icon": "🍳", "order": 2},
            {"name": "Bedding & Pillows",    "name_ar": "فراش ومخدات",           "icon": "🛏️", "order": 3},
            {"name": "Home Appliances",      "name_ar": "أجهزة منزلية",          "icon": "🧺", "order": 4},
            {"name": "Lighting",             "name_ar": "إضاءة",                 "icon": "💡", "order": 5},
            {"name": "Garden & Outdoor",     "name_ar": "حديقة وخارجي",         "icon": "🌿", "order": 6},
            {"name": "Tools & DIY",          "name_ar": "أدوات وصيانة",          "icon": "🔨", "order": 7},
            {"name": "Curtains & Decor",     "name_ar": "ستائر وديكور",          "icon": "🖼️", "order": 8},
            {"name": "Cleaning Supplies",    "name_ar": "مستلزمات تنظيف",       "icon": "🧹", "order": 9},
        ],
    },
    {
        "name": "Beauty & Health",
        "name_ar": "جمال وصحة",
        "icon": "💄",
        "order": 5,
        "children": [
            {"name": "Skincare",         "name_ar": "عناية بالبشرة",  "icon": "🧴", "order": 1},
            {"name": "Makeup",           "name_ar": "مكياج",           "icon": "💄", "order": 2},
            {"name": "Perfumes",         "name_ar": "عطور",            "icon": "🌸", "order": 3},
            {"name": "Hair Care",        "name_ar": "عناية بالشعر",   "icon": "💇", "order": 4},
            {"name": "Medical Devices",  "name_ar": "أجهزة طبية",     "icon": "🩺", "order": 5},
            {"name": "Vitamins & Supplements","name_ar":"فيتامينات ومكملات","icon":"💊","order": 6},
            {"name": "Personal Care",    "name_ar": "نظافة شخصية",    "icon": "🪥", "order": 7},
        ],
    },
    {
        "name": "Sports & Hobbies",
        "name_ar": "رياضة وهوايات",
        "icon": "⚽",
        "order": 6,
        "children": [
            {"name": "Football",         "name_ar": "كرة القدم",       "icon": "⚽", "order": 1},
            {"name": "Gym Equipment",    "name_ar": "معدات صالة رياضية","icon": "🏋️", "order": 2},
            {"name": "Outdoor & Camping","name_ar": "تخييم وهواء طلق", "icon": "⛺", "order": 3},
            {"name": "Swimming",         "name_ar": "سباحة",           "icon": "🏊", "order": 4},
            {"name": "Cycling",          "name_ar": "ركوب الدراجات",   "icon": "🚴", "order": 5},
            {"name": "Martial Arts",     "name_ar": "فنون قتالية",     "icon": "🥊", "order": 6},
            {"name": "Fishing",          "name_ar": "صيد الأسماك",     "icon": "🎣", "order": 7},
            {"name": "Board Games",      "name_ar": "ألعاب لوحية",     "icon": "♟️", "order": 8},
        ],
    },
    {
        "name": "Kids & Babies",
        "name_ar": "أطفال ورضع",
        "icon": "🧸",
        "order": 7,
        "children": [
            {"name": "Toys",             "name_ar": "ألعاب",                "icon": "🧸", "order": 1},
            {"name": "Baby Gear",        "name_ar": "عربات ومستلزمات رضع", "icon": "🍼", "order": 2},
            {"name": "Kids Furniture",   "name_ar": "أثاث أطفال",          "icon": "🛏️", "order": 3},
            {"name": "Educational Toys", "name_ar": "ألعاب تعليمية",       "icon": "🎲", "order": 4},
            {"name": "Kids Books",       "name_ar": "كتب الأطفال",         "icon": "📖", "order": 5},
        ],
    },
    {
        "name": "Books & Education",
        "name_ar": "كتب وتعليم",
        "icon": "📚",
        "order": 8,
        "children": [
            {"name": "School Books",     "name_ar": "كتب مدرسية",       "icon": "📚", "order": 1},
            {"name": "University Books", "name_ar": "كتب جامعية",       "icon": "🎓", "order": 2},
            {"name": "Islamic Books",    "name_ar": "كتب إسلامية",      "icon": "📖", "order": 3},
            {"name": "Novels & Stories", "name_ar": "روايات وقصص",      "icon": "📕", "order": 4},
            {"name": "Stationery",       "name_ar": "قرطاسية",          "icon": "✏️", "order": 5},
            {"name": "Online Courses",   "name_ar": "دورات إلكترونية",  "icon": "💻", "order": 6},
        ],
    },
    {
        "name": "Real Estate",
        "name_ar": "عقارات",
        "icon": "🏘️",
        "order": 9,
        "children": [
            {"name": "Apartments",       "name_ar": "شقق سكنية",    "icon": "🏢", "order": 1},
            {"name": "Houses",           "name_ar": "بيوت ودور",    "icon": "🏠", "order": 2},
            {"name": "Land",             "name_ar": "أراضي",        "icon": "🌍", "order": 3},
            {"name": "Commercial",       "name_ar": "تجاري",        "icon": "🏪", "order": 4},
            {"name": "Farms",            "name_ar": "مزارع",        "icon": "🌾", "order": 5},
        ],
    },
    {
        "name": "Art & Collectibles",
        "name_ar": "فنون ومقتنيات",
        "icon": "🎨",
        "order": 10,
        "children": [
            {"name": "Artwork & Paintings","name_ar":"لوحات فنية",   "icon": "🖼️", "order": 1},
            {"name": "Antiques",          "name_ar": "تحف وأنتيكات","icon": "🏺", "order": 2},
            {"name": "Coins & Stamps",    "name_ar": "عملات وطوابع","icon": "🪙", "order": 3},
            {"name": "Handmade Crafts",   "name_ar": "حرف يدوية",   "icon": "🧶", "order": 4},
        ],
    },
    {
        "name": "Animals & Pets",
        "name_ar": "حيوانات أليفة",
        "icon": "🐾",
        "order": 11,
        "children": [
            {"name": "Dogs",             "name_ar": "كلاب",                 "icon": "🐕", "order": 1},
            {"name": "Cats",             "name_ar": "قطط",                  "icon": "🐱", "order": 2},
            {"name": "Birds",            "name_ar": "طيور",                 "icon": "🦜", "order": 3},
            {"name": "Fish & Aquarium",  "name_ar": "أسماك وأحواض",        "icon": "🐠", "order": 4},
            {"name": "Pet Supplies",     "name_ar": "مستلزمات حيوانات",    "icon": "🦴", "order": 5},
        ],
    },
    {
        "name": "Food & Groceries",
        "name_ar": "طعام وبقالة",
        "icon": "🛒",
        "order": 12,
        "children": [
            {"name": "Fresh Produce",    "name_ar": "خضار وفواكه",   "icon": "🥕", "order": 1},
            {"name": "Packaged Food",    "name_ar": "معلبات وحبوب",  "icon": "🥫", "order": 2},
            {"name": "Dairy & Eggs",     "name_ar": "ألبان وبيض",    "icon": "🥚", "order": 3},
            {"name": "Sweets & Snacks",  "name_ar": "حلويات ومكسرات","icon": "🍬", "order": 4},
            {"name": "Beverages",        "name_ar": "مشروبات",       "icon": "🥤", "order": 5},
            {"name": "Spices & Herbs",   "name_ar": "توابل وأعشاب",  "icon": "🌶️", "order": 6},
        ],
    },
]


def make_slug(name, suffix=""):
    base = slugify(name)
    return f"{base}{suffix}" if suffix else base


class Command(BaseCommand):
    help = "Seed the database with professional shopping categories and subcategories"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing categories before seeding",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            deleted, _ = Category.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing categories."))

        created_parents = 0
        created_children = 0
        skipped = 0

        for cat_data in CATEGORIES:
            children = cat_data.pop("children", [])
            slug = make_slug(cat_data["name"])

            # Ensure slug uniqueness
            suffix = 1
            original_slug = slug
            while Category.objects.filter(slug=slug).exclude(name=cat_data["name"]).exists():
                slug = f"{original_slug}-{suffix}"
                suffix += 1

            parent, created = Category.objects.update_or_create(
                name=cat_data["name"],
                defaults={
                    "name_ar": cat_data["name_ar"],
                    "icon": cat_data["icon"],
                    "order": cat_data["order"],
                    "slug": slug,
                    "is_active": True,
                    "parent": None,
                },
            )
            if created:
                created_parents += 1
                self.stdout.write(f"  [+] {cat_data['name']} ({len(children)} subcategories)")
            else:
                skipped += 1
                self.stdout.write(f"  [~] {cat_data['name']} (updated)")

            for child in children:
                child_slug = make_slug(f"{cat_data['name']}-{child['name']}")
                suffix = 1
                original_child_slug = child_slug
                while Category.objects.filter(slug=child_slug).exclude(name=child["name"]).exists():
                    child_slug = f"{original_child_slug}-{suffix}"
                    suffix += 1

                _, child_created = Category.objects.update_or_create(
                    name=child["name"],
                    parent=parent,
                    defaults={
                        "name_ar": child["name_ar"],
                        "icon": child["icon"],
                        "order": child["order"],
                        "slug": child_slug,
                        "is_active": True,
                    },
                )
                if child_created:
                    created_children += 1

            # Restore children list (in case of future reuse)
            cat_data["children"] = children

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone! Created {created_parents} parent categories, "
                f"{created_children} subcategories. "
                f"Updated {skipped} existing."
            )
        )
