"""تبسيط حالة المنتج من خمس درجات إلى أربع.

`choices` قيد تحقّق في بايثون لا في MySQL، فـ AlterField وحده لا يمسّ صفاً
واحداً: الصفوف القديمة تبقى تحمل 'good' و'fair' و'poor' وتُعرض فارغة في
النماذج، وتفشل عند أول حفظ يمرّ بـ full_clean. الترحيل أدناه يحوّلها.

الثلاث كلها تُصبح 'used': لم تكن تحمل فرقاً يُحتجّ به — ولا منطق في الخادم
يقرأها — فدمجها لا يُفقد معلومة، والمحاولة العكسية (توزيعها على درجات) كانت
ستخترع تمييزاً لم يقصده البائع.
"""
from django.db import migrations, models

# ما لم يعد ضمن الخيارات -> بديله.
RETIRED = {'good': 'used', 'fair': 'used', 'poor': 'used'}


def narrow(apps, schema_editor):
    Item = apps.get_model('core', 'Item')
    for old, new in RETIRED.items():
        Item.objects.filter(condition=old).update(condition=new)


def widen(apps, schema_editor):
    """عكس غير دقيق بالضرورة: 'used' كانت ثلاث قيم، وإعادتها إلى 'good'
    تختار أوسطها. لا سبيل لاستعادة الأصل — الدمج يفقد التمييز بتعريفه —
    لكن وجود مسار رجوع يُبقي المهاجرة قابلة للتراجع."""
    Item = apps.get_model('core', 'Item')
    Item.objects.filter(condition='used').update(condition='good')
    Item.objects.filter(condition='unknown').update(condition='good')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0065_location_country_default'),
    ]

    operations = [
        migrations.AlterField(
            model_name='item',
            name='condition',
            field=models.CharField(
                choices=[('new', 'New'), ('like_new', 'Like New'),
                         ('used', 'Used'), ('unknown', 'Unknown')],
                max_length=20),
        ),
        migrations.RunPython(narrow, widen),
    ]
