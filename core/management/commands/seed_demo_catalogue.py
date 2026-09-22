"""Replace the leftover test rows with a presentable demo catalogue.

The database shipped with whatever the test runs left behind: items titled
'test', 'testtttt', 'jh' and 'bag' with descriptions like 'bag for men', every
store called "userN's Store", and — on one item — a photo of an Iraqi ID card.
None of that can be shown to anyone.

Items are rewritten IN PLACE rather than deleted and recreated. Their primary
keys are referenced by reservations, favourites, auction listings and the
product tag on a reel; recreating them would break those links, and the demo
only needs each item's presentation to change, not its identity.

`status` is deliberately left alone for the same reason: an item is 'sold'
because a reservation says so, and flipping it to 'active' would contradict
rows this command never looks at.

Photos come from Openverse filtered to commercially-licensed results, so the
seed data carries no licence problem. Each query is searched once and its
results are handed out across the products that share it.

Idempotent: running it again rewrites the same rows and refetches the photos.
"""
import time

import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Category, FixedPriceListing, Item, ItemImage, UserStore

OPENVERSE = "https://api.openverse.org/v1/images/"
UA = "BallaDemoSeed/1.0 (marketplace demo catalogue)"

# (name, description, logo query, cover query)
STORES = [
    ("متجر الرافدين للإلكترونيات",
     "أجهزة ذكية وإكسسوارات أصلية بضمان سنة. توصيل لكل المحافظات.",
     "electronics shop logo", "electronics store interior"),
    ("بيت الأناقة",
     "أزياء نسائية ورجالية بأحدث الموديلات، قطع محدودة تصل أسبوعياً.",
     "fashion boutique logo", "clothing boutique interior"),
    ("الشورجة للجملة",
     "بيع بالجملة والمفرد من قلب سوق الشورجة، أسعار تنافسية للتجّار.",
     "wholesale market logo", "wholesale market stall"),
    ("دار الكتاب العربي",
     "كتب دراسية وأدبية وإسلامية، ومستلزمات قرطاسية للطلاب.",
     "bookstore logo", "bookstore shelves"),
    ("سوق الفرات للمنزل",
     "كل ما يخص المنزل من أثاث وأدوات مطبخ ومفروشات.",
     "home goods shop logo", "home furniture showroom"),
    ("عطور بغداد",
     "عطور شرقية وفرنسية أصلية، وتركيبات خاصة تُحضّر عند الطلب.",
     "perfume shop logo", "perfume shop display"),
    ("الرياضي للمعدات",
     "معدات رياضية ومستلزمات النوادي واللياقة البدنية.",
     "sports shop logo", "gym equipment store"),
    ("عالم الأطفال",
     "ألعاب تعليمية ومستلزمات الأطفال والرضّع بمواصفات آمنة.",
     "toy store logo", "toy store shelves"),
    ("النخيل للمواد الغذائية",
     "تمور عراقية وبهارات ومواد غذائية طازجة من المنتج مباشرة.",
     "grocery shop logo", "spice market stall"),
    ("تكنو زون",
     "لابتوبات وأجهزة ألعاب وملحقات الحاسوب بأسعار الجملة.",
     "computer shop logo", "computer store interior"),
    ("لمسة جمال",
     "مستحضرات عناية بالبشرة والشعر ومكياج من علامات عالمية.",
     "cosmetics shop logo", "cosmetics store display"),
    ("ورشة الحرفي",
     "أعمال يدوية وتحف ولوحات فنية من صنع حرفيين عراقيين.",
     "handmade crafts logo", "artisan workshop"),
    ("أثاث دجلة",
     "أثاث منزلي ومكتبي بتصاميم عصرية وخامات مضمونة.",
     "furniture shop logo", "furniture showroom"),
    ("صيدلية الحياة",
     "أجهزة طبية منزلية ومكمّلات غذائية ومستلزمات العناية.",
     "pharmacy logo", "pharmacy interior"),
    ("حديقة الزهراء",
     "نباتات زينة ومستلزمات الحدائق والحيوانات الأليفة.",
     "garden shop logo", "garden center plants"),
]

# (title, name_ar, description, category name, condition, price IQD, image query)
PRODUCTS = [
    ("آيفون 15 برو ماكس 256 غيغابايت", "آيفون 15 برو ماكس",
     "هاتف بحالة ممتازة، استعمال ثمانية أشهر مع العلبة الأصلية والشاحن. البطارية بصحة 94% والشاشة بلا خدوش.",
     "Mobile Phones", "like_new", 1450000, "smartphone"),
    ("سامسونغ غالاكسي S23 ألترا", "غالاكسي S23 ألترا",
     "نسخة 512 غيغابايت بلون أسود، مع قلم S Pen. مستعمل بعناية وبدون أي كسور.",
     "Mobile Phones", "good", 1150000, "samsung galaxy phone"),
    ("لابتوب ديل XPS 15 بمعالج i7", "لابتوب ديل XPS 15",
     "شاشة 15.6 إنش بدقة عالية، رام 16 غيغا وقرص SSD سعة تيرابايت. مناسب للتصميم والبرمجة.",
     "Laptops & Computers", "good", 1800000, "laptop computer desk"),
    ("ماك بوك اير M2 جديد", "ماك بوك اير M2",
     "جهاز جديد بالكرتون غير مفتوح، ضمان الوكيل سنة كاملة. لون ميدنايت.",
     "Laptops & Computers", "new", 2100000, "macbook laptop"),
    ("آيباد الجيل العاشر مع غطاء", "آيباد الجيل العاشر",
     "سعة 64 غيغابايت واي فاي، مع غطاء جلدي وواقي شاشة مركّب.",
     "Tablets", "like_new", 620000, "tablet computer"),
    ("شاشة سمارت 55 إنش بدقة 4K", "شاشة سمارت 55 إنش",
     "شاشة LED بدقة 4K ونظام أندرويد مدمج، مع ريموت وحامل جداري.",
     "TV & Audio", "new", 780000, "flat screen television"),
    ("سماعات بلوتوث لاسلكية", "سماعات لاسلكية",
     "عزل ضوضاء نشط وعمر بطارية 30 ساعة مع علبة الشحن. صوت نقي ومريحة للأذن.",
     "TV & Audio", "new", 145000, "wireless headphones"),
    ("كاميرا كانون EOS 90D مع عدسة", "كاميرا كانون EOS 90D",
     "بودي بحالة ممتازة مع عدسة 18-135 ملم، عدد الغالقات أقل من 12 ألف.",
     "Cameras", "good", 1650000, "dslr camera"),
    ("بلايستيشن 5 مع يدّتين", "بلايستيشن 5",
     "نسخة الديسك مع يدّتين أصليتين وثلاث ألعاب. الجهاز نظيف ويعمل بلا مشاكل.",
     "Gaming", "like_new", 950000, "game console controller"),
    ("لوحة مفاتيح ميكانيكية RGB", "كيبورد ميكانيكي",
     "مفاتيح زرقاء بإضاءة قابلة للتخصيص، مناسبة للألعاب والكتابة الطويلة.",
     "Electronic Accessories", "new", 85000, "computer keyboard"),
    ("طابعة HP ليزر أحادية اللون", "طابعة HP ليزر",
     "طابعة مكتبية سريعة مع خرطوشة ممتلئة، مناسبة للمكاتب الصغيرة.",
     "Printers & Scanners", "good", 240000, "laser printer"),
    ("راوتر واي فاي 6 ثنائي النطاق", "راوتر واي فاي 6",
     "تغطية واسعة وأربعة هوائيات، يدعم حتى 64 جهازاً في وقت واحد.",
     "Networking", "new", 130000, "wifi router"),
    ("تويوتا كورولا 2018 فل", "تويوتا كورولا 2018",
     "ماشية 96 ألف كيلومتر، فحص كامل وبلا حوادث. صبغة وكالة ومكيّف بارد.",
     "Cars", "good", 18500000, "toyota sedan car"),
    ("دراجة نارية هوندا 150 سي سي", "دراجة هوندا 150",
     "موديل حديث بحالة جيدة، صيانة دورية موثّقة ومع خوذة أصلية.",
     "Motorcycles", "good", 2400000, "motorcycle"),
    ("طقم إطارات 17 إنش", "إطارات 17 إنش",
     "أربع إطارات بحالة ممتازة، نسبة التآكل أقل من 15%. مقاس 225/45.",
     "Car Parts", "good", 520000, "car tire wheel"),
    ("حامل هاتف مغناطيسي للسيارة", "حامل هاتف للسيارة",
     "تثبيت قوي على فتحة التكييف، يدور 360 درجة ويناسب كل أحجام الهواتف.",
     "Car Accessories", "new", 15000, "car phone holder"),
    ("دراجة هوائية جبلية 26 إنش", "دراجة جبلية",
     "هيكل ألمنيوم خفيف مع 21 سرعة ومكابح قرصية أمامية وخلفية.",
     "Bicycles", "like_new", 310000, "mountain bicycle"),
    ("قميص رجالي قطن مصري", "قميص رجالي قطن",
     "قطن 100% بقصّة كلاسيكية، متوفر بالمقاسات من M إلى XXL.",
     "Men's Clothing", "new", 35000, "dress shirt"),
    ("فستان سهرة مطرّز", "فستان سهرة",
     "تطريز يدوي على قماش شيفون، مناسب للمناسبات. متوفر بثلاثة ألوان.",
     "Women's Clothing", "new", 125000, "evening dress"),
    ("طقم ملابس أطفال قطني", "طقم أطفال قطني",
     "ثلاث قطع من قطن ناعم لا يهيّج البشرة، للأعمار من سنة إلى أربع.",
     "Kids' Clothing", "new", 28000, "kids clothes"),
    ("حذاء رياضي أصلي", "حذاء رياضي",
     "نعل مريح للمشي الطويل، مقاسات 40 إلى 45. أصلي بالكرتون.",
     "Shoes", "new", 165000, "sneakers"),
    ("حقيبة يد جلد طبيعي", "حقيبة يد جلدية",
     "جلد طبيعي مدبوغ بحرفية عالية، بحزام كتف قابل للفصل وجيوب داخلية.",
     "Bags", "new", 95000, "leather handbag"),
    ("حقيبة ظهر للطلاب مقاومة للماء", "حقيبة ظهر مدرسية",
     "قماش مقاوم للماء وجيب مبطّن للابتوب حتى 15 إنش. أحزمة مبطّنة مريحة.",
     "Bags", "new", 42000, "backpack bag"),
    ("ساعة يد كلاسيكية بسوار جلد", "ساعة يد كلاسيكية",
     "حركة كوارتز يابانية ومقاومة للماء، بسوار جلد بنّي وميناء أبيض.",
     "Watches", "new", 78000, "wrist watch"),
    ("طقم ذهب عيار 21", "طقم ذهب عيار 21",
     "سلسلة وأسورة بتصميم عراقي تقليدي، مع شهادة عيار من الصائغ.",
     "Jewelry", "new", 2800000, "gold necklace"),
    ("عباءة خليجية بتطريز يدوي", "عباءة خليجية",
     "قماش كريب فاخر مع تطريز يدوي على الأكمام. متوفرة بعدة مقاسات.",
     "Abayas & Thobes", "new", 110000, "abaya"),
    ("نظارة شمسية بولاريزد", "نظارة شمسية",
     "عدسات بولاريزد تحجب الأشعة فوق البنفسجية، بإطار معدني خفيف.",
     "Sunglasses", "new", 48000, "sunglasses"),
    ("طقم كنب زاوية سبعة مقاعد", "كنب زاوية",
     "قماش مقاوم للبقع مع إسفنج عالي الكثافة. يُسلّم ويُركّب في المنزل.",
     "Furniture", "new", 1250000, "sofa living room"),
    ("طقم أواني طبخ ستانلس ستيل", "طقم أواني طبخ",
     "عشر قطع من ستانلس ستيل بقاع حراري، تصلح لكل أنواع المواقد.",
     "Kitchen & Dining", "new", 185000, "cookware pots"),
    ("طقم مفارش سرير قطن", "مفارش سرير قطن",
     "ستّ قطع لسرير مزدوج من قطن مصري، ألوان ثابتة لا تبهت بالغسل.",
     "Bedding & Pillows", "new", 72000, "bed linen bedroom"),
    ("غسالة أوتوماتيك 8 كيلو", "غسالة 8 كيلو",
     "غسالة أمامية بضمان الوكيل سنتين وبرامج غسيل متعددة موفّرة للماء.",
     "Home Appliances", "new", 640000, "washing machine"),
    ("ثلاجة نوفروست بابين", "ثلاجة بابين",
     "سعة 500 لتر بتقنية النوفروست، موفّرة للطاقة وبضمان خمس سنوات للكمبريسر.",
     "Home Appliances", "new", 980000, "refrigerator kitchen"),
    ("ثريا كريستال لغرفة الجلوس", "ثريا كريستال",
     "كريستال حقيقي بإضاءة LED ثلاثية الدرجات، تناسب السقوف العالية.",
     "Lighting", "new", 340000, "chandelier light"),
    ("طقم أثاث حديقة راتان", "أثاث حديقة راتان",
     "طاولة وأربعة كراسي من الراتان الصناعي المقاوم للشمس والمطر.",
     "Garden & Outdoor", "new", 420000, "garden furniture patio"),
    ("طقم عدّة يدوية 120 قطعة", "عدّة يدوية 120 قطعة",
     "صندوق عدّة متكامل بمفاتيح ومفكات وبنسات من فولاذ كروم فاناديوم.",
     "Tools & DIY", "new", 135000, "tool set toolbox"),
    ("ستائر بلاك أوت لغرفة النوم", "ستائر بلاك أوت",
     "تحجب الضوء بالكامل وتعزل الحرارة، مع شريط تعليق جاهز للتركيب.",
     "Curtains & Decor", "new", 88000, "curtains window"),
    ("سيروم فيتامين سي للوجه", "سيروم فيتامين سي",
     "يوحّد لون البشرة ويقلّل آثار التصبّغ، خالٍ من الكحول والعطور.",
     "Skincare", "new", 32000, "skincare serum bottle"),
    ("باليت ظلال عيون 18 لون", "باليت ظلال عيون",
     "ألوان مطفية ولامعة بثبات طويل، تركيبة ناعمة سهلة الدمج.",
     "Makeup", "new", 45000, "makeup palette"),
    ("عطر شرقي عود ومسك 100 مل", "عطر عود ومسك",
     "تركيبة شرقية دافئة بثبات يتجاوز ثماني ساعات. مناسب للسهرات.",
     "Perfumes", "new", 155000, "perfume bottle"),
    ("مجفف شعر احترافي 2200 واط", "مجفف شعر احترافي",
     "ثلاث درجات حرارة وفوهة تصفيف دقيقة، مع تقنية الأيونات لتقليل التجعّد.",
     "Hair Care", "new", 68000, "hair dryer"),
    ("جهاز قياس ضغط الدم الرقمي", "جهاز قياس الضغط",
     "قياس دقيق مع ذاكرة لآخر 90 قراءة وشاشة كبيرة واضحة. معتمد طبياً.",
     "Medical Devices", "new", 55000, "blood pressure monitor"),
    ("كرة قدم مقاس 5 مخيطة يدوياً", "كرة قدم مقاس 5",
     "جلد صناعي مقاوم للتآكل، مناسبة للملاعب العشبية والترابية.",
     "Football", "new", 25000, "football soccer ball"),
    ("طقم دمبل قابل للتعديل 20 كغم", "دمبل قابل للتعديل",
     "أوزان قابلة للتغيير من 2 إلى 20 كيلوغرام، مع حامل تخزين.",
     "Gym Equipment", "new", 195000, "dumbbell weights gym"),
    ("خيمة تخييم لأربعة أشخاص", "خيمة تخييم",
     "مقاومة للماء وسهلة النصب خلال دقائق، مع حقيبة حمل وأوتاد.",
     "Outdoor & Camping", "new", 175000, "camping tent"),
    ("خوذة دراجات هوائية", "خوذة دراجات",
     "خفيفة الوزن مع فتحات تهوية وحزام قابل للتعديل. معتمدة للسلامة.",
     "Cycling", "new", 38000, "bicycle helmet"),
    ("طقم صنارة صيد مع بكرة", "صنارة صيد مع بكرة",
     "صنارة كربون بطول 2.7 متر مع بكرة دوّارة وخيط جاهز.",
     "Fishing", "new", 92000, "fishing rod reel"),
    ("سيارة تحكّم عن بعد قابلة للشحن", "سيارة تحكّم عن بعد",
     "بطارية قابلة للشحن ومدى تحكّم 50 متراً، للأعمار فوق ست سنوات.",
     "Toys", "new", 45000, "remote control toy car"),
    ("عربة أطفال قابلة للطي", "عربة أطفال",
     "طي بيد واحدة ووزن خفيف، مع مظلة شمس وسلة تخزين واسعة.",
     "Baby Gear", "like_new", 210000, "baby stroller"),
    ("مكعبات بناء تعليمية 200 قطعة", "مكعبات بناء تعليمية",
     "تنمّي المهارات الحركية والتفكير الهندسي، بلاستيك آمن خالٍ من BPA.",
     "Educational Toys", "new", 52000, "building blocks toys"),
    ("مجموعة قصص أطفال مصوّرة", "قصص أطفال مصوّرة",
     "عشر قصص عربية مصوّرة بورق سميك، مناسبة لأعمار 4 إلى 8 سنوات.",
     "Kids Books", "new", 30000, "children books"),
    ("موسوعة الرياضيات للسادس الإعدادي", "موسوعة الرياضيات",
     "شرح مبسّط مع أسئلة الوزاري لعشر سنوات وحلولها النموذجية.",
     "School Books", "good", 22000, "mathematics textbook"),
    ("مرجع التشريح الطبي", "مرجع التشريح الطبي",
     "طبعة حديثة بحالة ممتازة، بلا تظليل أو تمزّق. مرجع أساسي لطلبة الطب.",
     "University Books", "good", 65000, "medical anatomy book"),
    ("مصحف بخط عثماني مجلّد", "مصحف مجلّد",
     "طباعة واضحة بورق شمواه وتجليد فاخر، مقاس متوسط مناسب للحمل.",
     "Islamic Books", "new", 35000, "quran islamic book"),
    ("رواية عربية معاصرة", "رواية عربية",
     "طبعة أولى بحالة ممتازة من دار نشر معروفة، 320 صفحة.",
     "Novels & Stories", "like_new", 18000, "arabic novel book"),
    ("طقم قرطاسية للمكتب", "طقم قرطاسية",
     "دفاتر وأقلام ومنظّم مكتبي بتصميم أنيق، مناسب للطلاب والموظفين.",
     "Stationery", "new", 26000, "stationery desk supplies"),
    ("لوحة فنية مرسومة يدوياً", "لوحة فنية يدوية",
     "ألوان زيتية على قماش بمقاس 60×80 سم، بإطار خشبي جاهز للتعليق.",
     "Artwork & Paintings", "new", 180000, "oil painting canvas"),
    ("إبريق نحاسي أثري", "إبريق نحاسي أثري",
     "قطعة نحاسية مشغولة يدوياً يعود عمرها لأكثر من خمسين عاماً.",
     "Antiques", "good", 145000, "antique copper pot"),
    ("سجادة صوف منسوجة يدوياً", "سجادة صوف يدوية",
     "نسيج يدوي من صوف طبيعي بألوان نباتية ثابتة، مقاس 150×200 سم.",
     "Handmade Crafts", "new", 390000, "handmade wool rug"),
    ("قفص طيور معدني كبير", "قفص طيور كبير",
     "معدن مطلي مقاوم للصدأ مع مجاثم وأوعية طعام وماء وقاعدة سهلة التنظيف.",
     "Pet Supplies", "new", 75000, "bird cage"),
    ("تمر برحي عراقي درجة أولى", "تمر برحي عراقي",
     "تمر طازج من بساتين البصرة، معبّأ بعناية في علب سعة كيلوغرام.",
     "Fresh Produce", "new", 12000, "dates fruit"),
    ("عسل جبلي طبيعي كيلوغرام", "عسل جبلي طبيعي",
     "عسل خام غير مبستر من مناحل كردستان، بشهادة فحص مختبري.",
     "Packaged Food", "new", 45000, "honey jar"),
    ("بقلاوة بالفستق الحلبي", "بقلاوة بالفستق",
     "تُحضّر يومياً بالسمن الطبيعي والفستق الحلبي، علبة كيلوغرام.",
     "Sweets & Snacks", "new", 28000, "baklava dessert"),
    ("قهوة عربية مطحونة بالهيل", "قهوة عربية بالهيل",
     "بنّ عربي محمّص على الطريقة التقليدية مع هيل أخضر، نصف كيلوغرام.",
     "Beverages", "new", 20000, "arabic coffee beans"),
    ("طقم بهارات عراقية مشكّلة", "بهارات عراقية مشكّلة",
     "ثمانية أنواع من البهارات المطحونة طازجة، في علب زجاجية محكمة.",
     "Spices & Herbs", "new", 24000, "spices jars"),
    ("شقة 140 متر في المنصور", "شقة في المنصور",
     "ثلاث غرف نوم وصالة واسعة، الطابق الثالث مع مصعد وموقف خاص.",
     "Apartments", "good", 185000000, "apartment building modern"),
]


# Openverse is mostly amateur Flickr photography, which answers a product query
# with whatever happens to contain the word — "tablet" returns a child lit by a
# screen, "keyboard" a man at a market stall. Rawpixel is the one curated stock
# library in the index, so it is tried first and the wider pool is only a
# fallback. Restricting to jpg drops most of Rawpixel's cutout stickers.
# Sources are pinned, not left open. Openverse also indexes museum and heritage
# collections (Met, Europeana, Smithsonian, Science Museum…), and their titles
# match product words so precisely that "men's shirt" returns a 1937 fireman's
# tunic and "kids clothing" a black-and-white group portrait. Rawpixel is the
# curated stock library; Flickr supplies ordinary photographs of ordinary
# objects, which is exactly what a second-hand listing looks like.
SEARCH_TIERS = (
    {"source": "rawpixel", "extension": "jpg"},
    {"source": "flickr", "extension": "jpg"},
    {"source": "rawpixel,flickr"},
)

# Titles that mean the result is artwork rather than a photograph of the thing.
NOT_A_PHOTO = ("clipart", "sticker", "illustration", "vector", "icon",
               "drawing", "png", "collage", "pattern", "sketch")


# One distinct Arabic letter per store, drawn from its name. Single letters are
# used on purpose: rendering joined Arabic needs a reshaper this project doesn't
# install, while an isolated letter is already its correct glyph.
MONOGRAMS = ["ر", "ب", "ش", "ك", "ف", "ع", "ض", "ط",
             "ن", "ت", "ل", "ح", "د", "ص", "ز"]

# Warm tones around the app's own primary (#AB3500) so the store list reads as
# one family rather than fifteen unrelated brands.
LOGO_COLOURS = [
    (171, 53, 0), (196, 87, 24), (140, 62, 20), (168, 104, 32),
    (122, 45, 30), (188, 74, 40), (146, 88, 24), (110, 66, 40),
    (204, 108, 36), (132, 52, 12), (180, 96, 56), (96, 58, 26),
    (216, 122, 48), (128, 74, 18), (156, 66, 34),
]

LOGO_FONT = r"C:\Users\Eng_Joseph\Desktop\marketplace_app\assets\fonts\Amiri-Bold.ttf"


def monogram(letter, colour, size=512):
    """A flat tile carrying one letter — the store equivalent of an avatar."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (size, size), colour)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(LOGO_FONT, int(size * 0.55))
    except OSError:
        font = ImageFont.load_default()

    box = draw.textbbox((0, 0), letter, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0],
         (size - (box[3] - box[1])) / 2 - box[1]),
        letter, font=font, fill=(253, 240, 224),
    )

    from io import BytesIO
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def openverse_search(query, want=8):
    """Commercially-licensed photos only — seed images have to be safe to ship."""
    for extra in SEARCH_TIERS:
        params = {"q": query, "page_size": want, "license_type": "commercial",
                  "mature": "false"}
        params.update(extra)
        try:
            response = requests.get(OPENVERSE, params=params,
                                    headers={"User-Agent": UA}, timeout=30)
            response.raise_for_status()
            results = response.json().get("results", [])
        except Exception:
            continue

        # Openverse matches on tags and description as well as the title, which
        # is how "television" returns a portrait of a man in a suit. Requiring a
        # query word in the title, and ranking by how many match, throws those
        # out — the photos actually OF the product name it.
        tokens = [t for t in query.lower().split() if len(t) > 2]
        scored = []
        for r in results:
            title = (r.get("title") or "").lower()
            if not r.get("url") or any(w in title for w in NOT_A_PHOTO):
                continue
            hits = sum(1 for t in tokens if t in title)
            if hits:
                scored.append((hits, r["url"]))

        scored.sort(key=lambda pair: -pair[0])
        urls = [url for _, url in scored]
        # One usable hit is more likely a coincidence than a match; two means the
        # tier actually understood the query.
        if len(urls) >= 2:
            return urls
    return []


def fetch_bytes(url):
    response = requests.get(url, headers={"User-Agent": UA}, timeout=45)
    response.raise_for_status()
    data = response.content
    # Guards against HTML error pages served with a 200, and against thumbnails
    # too small to pass for a product photo.
    if not response.headers.get("Content-Type", "").startswith("image/"):
        raise ValueError("not an image: %s" % url)
    if len(data) < 8000:
        raise ValueError("too small: %s" % url)
    return data


class Command(BaseCommand):
    help = "Replace leftover test items/stores with a demo catalogue and real photos."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without writing anything.")
        parser.add_argument("--skip-images", action="store_true",
                            help="Rewrite text only; leave existing photos alone.")
        parser.add_argument("--stores-only", action="store_true",
                            help="Re-do the stores; leave item rows and photos untouched.")

    def handle(self, *args, **options):
        self._cache = {}
        items = list(Item.objects.order_by("id"))
        stores = list(UserStore.objects.order_by("id"))

        self.stdout.write("items: %d   stores: %d   products defined: %d"
                          % (len(items), len(stores), len(PRODUCTS)))

        if options["dry_run"]:
            for item, spec in zip(items, PRODUCTS):
                self.stdout.write("  item  #%-4s %-22s -> %s" % (item.id, item.title, spec[0]))
            for store, spec in zip(stores, STORES):
                self.stdout.write("  store #%-4s %-22s -> %s" % (store.id, store.name, spec[0]))
            return

        self.seed_stores(stores, options["skip_images"])
        if not options["stores_only"]:
            self.seed_items(items, options["skip_images"])
        self.stdout.write(self.style.SUCCESS("demo catalogue seeded"))

    # -- images ----------------------------------------------------------
    def image_for(self, query, index):
        """One search per query; its results are shared by the items using it."""
        if query not in self._cache:
            try:
                self._cache[query] = openverse_search(query)
            except Exception as exc:
                self.stderr.write("  search failed %r: %s" % (query, exc))
                self._cache[query] = []
            time.sleep(0.3)

        urls = self._cache[query]
        for offset in range(len(urls)):
            try:
                return fetch_bytes(urls[(index + offset) % len(urls)])
            except Exception:
                continue
        return None

    # -- stores ----------------------------------------------------------
    def seed_stores(self, stores, skip_images):
        for index, store in enumerate(stores):
            name, description, logo_query, cover_query = STORES[index % len(STORES)]
            store.name = name
            store.description = description

            if not skip_images:
                # Logos are drawn, not searched. An image search for "bookstore
                # logo" returns real shop signage — a KIA badge, a Disney Store
                # front, an actual bookshop's wordmark — and putting a live
                # trademark on a made-up store is a legal problem, not just a
                # sloppy one. A monogram tile owes nobody anything and reads
                # cleanly at the size the app shows it.
                store.logo.save("store_%s_logo.png" % store.id,
                                ContentFile(monogram(MONOGRAMS[index % len(MONOGRAMS)],
                                                     LOGO_COLOURS[index % len(LOGO_COLOURS)])),
                                save=False)
                cover = self.image_for(cover_query, index)
                if cover:
                    store.cover_image.save("store_%s_cover.jpg" % store.id,
                                           ContentFile(cover), save=False)

            store.save()
            self.stdout.write("  store #%-3s -> %s" % (store.id, name))

    # -- items -----------------------------------------------------------
    def seed_items(self, items, skip_images):
        for index, item in enumerate(items):
            spec = PRODUCTS[index % len(PRODUCTS)]
            title, name_ar, description, category_name, condition, price, query = spec

            category = Category.objects.filter(name=category_name).first()
            if category is None:
                self.stderr.write("  no category %r; skipped item %s" % (category_name, item.id))
                continue

            with transaction.atomic():
                item.title = title
                item.name_ar = name_ar
                item.description = description
                item.category = category
                item.condition = condition
                item.save(update_fields=["title", "name_ar", "description",
                                         "category", "condition"])

                # The app reads a price from the fixed-price listing, so an item
                # without one renders blank. Auction items are left alone —
                # they price through bids, not this row.
                if not hasattr(item, "auction_listing"):
                    FixedPriceListing.objects.update_or_create(
                        item=item,
                        defaults={"price": price, "deposit_percentage": 20,
                                  "is_active": True},
                    )

                if not skip_images:
                    for old in item.images.all():
                        old.image.delete(save=False)
                        old.delete()
                    data = self.image_for(query, index)
                    if data:
                        image = ItemImage(item=item, order=0)
                        image.image.save("item_%s_0.jpg" % item.id,
                                         ContentFile(data), save=False)
                        image.save()

            self.stdout.write("  item  #%-4s -> %s" % (item.id, title))
