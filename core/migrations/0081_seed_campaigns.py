from django.db import migrations

# code, name, audience, headline, lede, points
CAMPAIGNS = [
    ('c1', 'زبائن — السوق في جيبك', 'customer', 'السوق كلّه في جيبك',
     'متاجر العراق في تطبيق واحد: تصفّح، قارن، واحجز وأنت في مكانك.',
     'تصفّح بلا حساب\nبضاعة من متاجر عراقية\nتوصيل إلى كل المحافظات'),
    ('c2', 'زبائن — المزادات والبث', 'customer', 'زايد، وخذها بسعرك',
     'مزادات وبثّ مباشر من متاجر العراق، تتابعها وتزايد من هاتفك.',
     'مزادات تتابعها لحظة بلحظة\nبثّ مباشر ترى فيه البضاعة\nسعرك أنت من يحدّده'),
    ('c3', 'زبائن — تسوّق باطمئنان', 'customer', 'اطلبها، وافحصها عند بابك',
     'مبلغك محفوظ حتى تؤكّد الاستلام، وطلبك تتابعه خطوة بخطوة.',
     'المبلغ لا يصل البائع قبل تأكيدك\nأجرة التوصيل تعرفها قبل التأكيد\nتتبّع الطلب حتى الباب'),
    ('m1', 'بائعون — افتح متجرك', 'merchant', 'متجرك مفتوح لكل العراق',
     'افتح متجرك في الشورجة واعرض بضاعتك لزبائن كل المحافظات.',
     'صفحة متجر باسمك\nطلبات وحجوزات في مكان واحد\nتوصيل إلى كل المحافظات'),
    ('m2', 'بائعون — بث ومزاد', 'merchant', 'بِع وأنت على الهواء',
     'بثّ مباشر وريلز ومزادات من هاتفك، والزبون يحجز وهو يشاهد.',
     'بثّ مباشر من هاتفك\nريلز تعرض بضاعتك\nمزادات لقطعك المميّزة'),
    ('m3', 'بائعون — جملة ومندوبون', 'merchant', 'وسّع تجارتك بلا محلّ ثانٍ',
     'بِع بالجملة لتجّار آخرين، ودع المندوبين يسوّقون بضاعتك بنسبة تتّفقون عليها.',
     'بيع بالجملة داخل التطبيق\nمندوبون بنسبة متّفق عليها\nمحفظة تتابع فيها أرباحك'),
]


def seed(apps, schema_editor):
    Campaign = apps.get_model('core', 'Campaign')
    for code, name, audience, headline, lede, points in CAMPAIGNS:
        Campaign.objects.get_or_create(code=code, defaults={
            'name': name, 'audience': audience, 'headline': headline, 'lede': lede,
            'points': points, 'image_name': code, 'has_book': True,
        })


class Migration(migrations.Migration):

    dependencies = [('core', '0080_campaigns')]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
