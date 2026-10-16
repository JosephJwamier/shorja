# -*- coding: utf-8 -*-
"""
seed_legal_documents
====================
يزرع/يحدّث سياسة الخصوصية وشروط الاستخدام (عربي + إنجليزي) في قاعدة البيانات.

    python manage.py seed_legal_documents                 # ينشئ الإصدار 1 إن لم يوجد
    python manage.py seed_legal_documents --update         # يحدّث محتوى أحدث إصدار مكانه
    python manage.py seed_legal_documents --bump           # ينشئ إصداراً جديداً ويُلزم بإعادة الموافقة
    python manage.py seed_legal_documents --entity "شركة كذا" --email support@shorjeh.com \
        --address "بغداد - ..." --city "بغداد" --website https://shorjeh.com

بيانات الشركة تُقرأ من الوسائط أعلاه أو من متغيّرات البيئة
(LEGAL_ENTITY / LEGAL_CONTACT_EMAIL / LEGAL_ADDRESS / LEGAL_CITY / LEGAL_WEBSITE)
وإلا تُستخدم عناصر نائبة واضحة بين «» يجب ملؤها قبل الإطلاق.

تنبيه: هذه صياغة احترافية عامة ونقطة انطلاق قوية، وليست بديلاً عن مراجعة
محامٍ عراقي مرخّص قبل الاعتماد للنشر في المتاجر أو الحملات المدفوعة.
"""
import os
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import LegalDocument


def _cfg(options):
    g = lambda k, d: (options.get(k) or os.environ.get(k.upper()) or d)
    return {
        'brand_ar': 'شورجة',
        'brand_en': 'Shorja',
        'entity_ar': g('entity', os.environ.get('LEGAL_ENTITY', '«يُملأ: الاسم القانوني المسجَّل للجهة المالكة»')),
        'entity_en': g('entity', os.environ.get('LEGAL_ENTITY', '«TO FILL: registered owning legal entity»')),
        'email': g('email', os.environ.get('LEGAL_CONTACT_EMAIL', 'support@shorjeh.com')),
        'website': g('website', os.environ.get('LEGAL_WEBSITE', 'https://shorjeh.com')),
        'address_ar': g('address', os.environ.get('LEGAL_ADDRESS', '«يُملأ: العنوان الكامل — العراق»')),
        'address_en': g('address', os.environ.get('LEGAL_ADDRESS', '«TO FILL: full registered address — Iraq»')),
        'city_ar': g('city', os.environ.get('LEGAL_CITY', '«يُملأ: مدينة الاختصاص، مثال: بغداد»')),
        'city_en': g('city', os.environ.get('LEGAL_CITY', '«TO FILL: jurisdiction city, e.g. Baghdad»')),
    }


# ============================================================================
# سياسة الخصوصية — العربية
# ============================================================================
def privacy_ar(c):
    title = 'سياسة الخصوصية'
    summary = 'كيف نجمع بياناتك ونستخدمها ونحميها في تطبيق شورجة.'
    html = f"""
<p>تُوضّح هذه السياسة كيف يقوم تطبيق ومنصّة <strong>{c['brand_ar']}</strong> (المُشار إليه بـ«نحن» أو «المنصّة»)،
المُشغَّل من قِبل {c['entity_ar']}، بجمع بياناتك الشخصية واستخدامها والإفصاح عنها وحمايتها عند استخدامك خدماتنا.
باستخدامك المنصّة فإنك تُقرّ بأنك قرأت هذه السياسة وفهمتها.</p>

<h2>1. المعلومات التي نجمعها</h2>
<h3>أ) معلومات تُقدّمها أنت</h3>
<ul>
  <li>بيانات الحساب: اسم المستخدم، البريد الإلكتروني، رقم الهاتف، وكلمة المرور (تُخزَّن مُشفّرة ولا يمكننا رؤيتها).</li>
  <li>بيانات المتجر والإعلانات: عنوان السلعة ووصفها وسعرها وصورها وفئتها وموقعها.</li>
  <li>محتوى التواصل: رسائل الدردشة، التقييمات والمراجعات، وتفاصيل النزاعات وما ترفقه بها.</li>
  <li>بيانات التحقق: ما قد نطلبه للتحقق من الهوية أو ملكية المتجر عند الحاجة.</li>
</ul>
<h3>ب) معلومات نجمعها تلقائياً</h3>
<ul>
  <li>بيانات الجهاز: النوع، نظام التشغيل، معرّف الجهاز، ولغة التطبيق.</li>
  <li>بيانات الاستخدام والسجلّات: عنوان IP، الصفحات والأقسام التي تزورها، وأوقات الوصول.</li>
  <li>رمز الإشعارات (FCM) لإرسال التنبيهات إليك.</li>
</ul>
<h3>ج) الموقع الجغرافي</h3>
<p>بعد إذنك، نستخدم موقع جهازك لعرض الإعلانات والمتاجر القريبة منك، وتحديد المحافظة، وتسهيل التوصيل.
يمكنك سحب هذا الإذن في أي وقت من إعدادات جهازك.</p>
<h3>د) المعلومات المالية والمعاملات</h3>
<ul>
  <li>رصيد المحفظة، وطلبات الإيداع والسحب، وسجلّ المعاملات والمزادات والعروض والمشتريات.</li>
  <li>عند الدفع أو السحب نعالج آخر أربعة أرقام فقط من البطاقة ومرجع العملية؛
      <strong>ولا نُخزّن رقم بطاقتك الكامل</strong> — تُعالَج بيانات الدفع لدى مزوّد الدفع الآمن.</li>
</ul>
<h3>هـ) تسجيل الدخول عبر مزوّد خارجي</h3>
<p>عند اختيارك الدخول عبر Google أو Facebook، نتلقّى منهما معرّفاً فريداً وبريدك واسمك العام وفق أذوناتك لديهما.</p>

<h2>2. كيف نستخدم معلوماتك</h2>
<ul>
  <li>إنشاء حسابك وتشغيله وتمكينك من النشر والشراء والبيع والمزايدة.</li>
  <li>معالجة المدفوعات والمحفظة والإيداع والسحب وحساب الرسوم والعمولات.</li>
  <li>عرض محتوى وإعلانات ومتاجر قريبة وملائمة لاهتماماتك.</li>
  <li>إرسال إشعارات تشغيلية (رسائل، مزايدات، حالة الطلبات) وإشعارات أمنية.</li>
  <li>الحماية من الاحتيال وإساءة الاستخدام، وضمان أمن المنصّة، وحلّ النزاعات.</li>
  <li>تحسين خدماتنا وتحليل الأداء ومعالجة الأعطال.</li>
  <li>الامتثال للالتزامات القانونية والتنظيمية.</li>
</ul>

<h2>3. الأساس القانوني للمعالجة</h2>
<p>نعالج بياناتك بالاستناد إلى: تنفيذ العقد معك (تقديم الخدمة)، وموافقتك (مثل الموقع والإشعارات التسويقية)،
ومصلحتنا المشروعة (الأمن ومنع الاحتيال وتحسين الخدمة)، والامتثال لالتزام قانوني.</p>

<h2>4. مشاركة المعلومات</h2>
<p>لا نبيع بياناتك الشخصية. وقد نُشاركها في الحدود اللازمة مع:</p>
<ul>
  <li><strong>مزوّدي الخدمة:</strong> معالِج الدفع (Stripe)، وخدمات Google Firebase (الإشعارات، التحليلات،
      تقارير الأعطال، والمصادقة)، وخدمة البث المباشر (Agora)، وخرائط OpenStreetMap، وشركات التوصيل،
      ومزوّدي الاستضافة والبريد — وذلك لتشغيل الخدمة نيابةً عنّا.</li>
  <li><strong>مستخدمين آخرين:</strong> ما تنشره علناً (الإعلانات، اسم المتجر، التقييمات) يكون مرئيّاً للآخرين،
      وتُشارَك معلومات التواصل اللازمة مع الطرف الآخر عند إتمام صفقة أو توصيل.</li>
  <li><strong>لأسباب قانونية:</strong> عند طلب مشروع من سلطة مختصة في جمهورية العراق، أو لحماية حقوقنا وسلامة المستخدمين.</li>
  <li><strong>عمليات نقل الأعمال:</strong> في حال اندماج أو استحواذ أو بيع أصول، مع بقاء بياناتك خاضعة لهذه السياسة.</li>
</ul>

<h2>5. الاحتفاظ بالبيانات</h2>
<p>نحتفظ ببياناتك طوال فعالية حسابك، ولمدة إضافية يقتضيها القانون أو تسوية النزاعات أو منع الاحتيال أو حفظ السجلّات المالية.
عند انتفاء الحاجة نحذفها أو نجعلها مجهولة الهوية.</p>

<h2>6. أمن المعلومات</h2>
<p>نطبّق إجراءات تقنية وتنظيمية معقولة تشمل تشفير كلمات المرور، ورموز الوصول المؤقتة (JWT)،
والتحقّق الثنائي للوحة الإدارة. ومع ذلك لا توجد وسيلة نقل أو تخزين آمنة بنسبة 100%، ولا يمكننا ضمان أمن مطلق.</p>

<h2>7. حقوقك</h2>
<p>يحقّ لك — ضمن ما تُتيحه الأنظمة المعمول بها — الوصول إلى بياناتك وتصحيحها أو تحديثها، وطلب حذفها،
وسحب موافقتك، والاعتراض على معالجة معيّنة. لممارسة هذه الحقوق تواصل معنا عبر البريد أدناه.</p>

<h2>8. حذف الحساب</h2>
<p>يمكنك حذف حسابك من داخل التطبيق عبر «الإعدادات». سيؤدي ذلك إلى إزالة أو إخفاء بياناتك الشخصية،
مع احتفاظنا بالحد الأدنى اللازم قانونياً (مثل السجلّات المالية أو ما يخصّ نزاعاً قائماً).</p>

<h2>9. خصوصية الأطفال</h2>
<p>خدماتنا مخصّصة لمن بلغوا <strong>18 عاماً</strong> فأكثر. لا نجمع عن قصد بيانات ممّن هم دون ذلك،
وإذا علِمنا بجمع بيانات قاصر دون سند نظامي، بادرنا إلى حذفها.</p>

<h2>10. الإشعارات والتسويق</h2>
<p>يمكنك إيقاف الإشعارات من إعدادات جهازك أو التطبيق. قد نرسل رسائل تشغيلية ضرورية لخدمتك حتى لو أوقفت الرسائل التسويقية.</p>

<h2>11. نقل البيانات دولياً</h2>
<p>قد تُعالَج بياناتك أو تُخزَّن على خوادم لدى مزوّدينا قد تقع خارج جمهورية العراق. نتّخذ خطوات معقولة لحمايتها وفق هذه السياسة.</p>

<h2>12. التعديلات على هذه السياسة</h2>
<p>قد نُحدّث هذه السياسة من وقت لآخر. عند إجراء تغييرات جوهرية سنُشعرك داخل التطبيق وقد نطلب موافقتك من جديد قبل متابعة الاستخدام.</p>

<h2>13. تواصل معنا</h2>
<p>لأي استفسار حول الخصوصية: البريد الإلكتروني <a href="mailto:{c['email']}">{c['email']}</a> —
الموقع <a href="{c['website']}">{c['website']}</a> — العنوان: {c['address_ar']}.</p>
"""
    return title, summary, html.strip()


# ============================================================================
# شروط الاستخدام — العربية
# ============================================================================
def terms_ar(c):
    title = 'شروط الاستخدام'
    summary = 'القواعد المنظِّمة لاستخدامك منصّة شورجة والبيع والشراء والمزادات فيها.'
    html = f"""
<p>تحكم هذه الشروط استخدامك تطبيق ومنصّة <strong>{c['brand_ar']}</strong> المُشغَّلة من قِبل {c['entity_ar']}.
بإنشائك حساباً أو استخدامك المنصّة فإنك توافق على هذه الشروط بالكامل. إن لم توافق، فالرجاء عدم استخدام المنصّة.</p>

<h2>1. التعريفات</h2>
<p>«المنصّة» تطبيق وخدمات شورجة. «المستخدم» كل من يُنشئ حساباً أو يستخدم المنصّة. «البائع» و«المشتري» طرفا المعاملة.
«المتجر» الملف التجاري للبائع. «المندوب» من يقدّم خدمة نيابة عن غيره عبر المنصّة. «المزاد» بيع بالمزايدة العلنية.</p>

<h2>2. الأهلية</h2>
<p>يجب أن يكون عمرك <strong>18 عاماً</strong> فأكثر وتتمتّع بالأهلية القانونية للتعاقد. باستخدامك المنصّة تُقرّ باستيفائك هذا الشرط.</p>

<h2>3. الحساب</h2>
<p>تلتزم بتقديم معلومات صحيحة ومحدّثة، وبالحفاظ على سرّية كلمة المرور، وتتحمّل مسؤولية كل نشاط يجري عبر حسابك.
أبلِغنا فوراً عند أي استخدام غير مصرّح به.</p>

<h2>4. طبيعة المنصّة</h2>
<p>شورجة وسيط تقني يتيح للبائعين عرض سلعهم وللمشترين التعامل معهم. العقد يُبرَم بين البائع والمشتري مباشرةً،
ولسنا مالكين للسلع ولا طرفاً في البيع، إلا في حدود ما تنص عليه خدمات محدّدة نقدّمها (كالمحفظة أو احتجاز المبالغ لحين إتمام الصفقة).
لا نضمن جودة السلع أو صحّة أوصافها أو قدرة الأطراف على الوفاء.</p>

<h2>5. النشر والإعلانات</h2>
<ul>
  <li>تضمن أنك تملك السلعة أو الحق في بيعها، وأن وصفها وصورها دقيقة وغير مضلِّلة.</li>
  <li>يُحظر عرض ما هو ممنوع قانوناً في جمهورية العراق، أو المقلَّد، أو المسروق، أو الخطير، أو المخالف للآداب العامة
      (بما في ذلك — على سبيل المثال لا الحصر — الأسلحة والمخدرات والمواد المحظورة والمنتجات المنتهكة لحقوق الغير).</li>
  <li>يحقّ لنا رفض أو إزالة أو تعديل أي إعلان يخالف هذه الشروط دون إشعار مسبق.</li>
</ul>

<h2>6. المزادات والعروض</h2>
<p>تقديمك عطاءً في مزاد أو عرضاً للشراء يُعدّ التزاماً جادّاً وقد يكون مُلزِماً. عند فوزك أو قبول عرضك تلتزم بإتمام الصفقة وفق شروطها.
قد تُطبَّق رسوم أو مبالغ تأمين، وتُوضَّح آليتها داخل التطبيق.</p>

<h2>7. المدفوعات والمحفظة والرسوم</h2>
<ul>
  <li>تُتيح المحفظة الإيداع والسحب وإجراء المعاملات داخل المنصّة، ويخضع السحب لمراجعة وموافقة إدارية.</li>
  <li>قد نفرض عمولات أو رسوم خدمة أو رسوم تخزين أو توصيل، وتُعرَض قبل إتمام العملية.</li>
  <li>تتحمّل أي ضرائب أو رسوم حكومية مستحقّة على معاملاتك. وقد نُعدّل الرسوم مستقبلاً بإشعار مسبق معقول.</li>
</ul>

<h2>8. المندوبون وإعادة البيع والتوصيل</h2>
<p>قد تتيح المنصّة خدمات المندوبين وإعادة البيع والتوصيل. يلتزم كل طرف بأداء دوره بأمانة ووفق الشروط المتّفق عليها،
وتبقى المنصّة وسيطاً منظِّماً لهذه الخدمات لا ضامناً لنتائجها ما لم يُنص على خلاف ذلك.</p>

<h2>9. البث المباشر</h2>
<p>عند استخدامك ميزة البث المباشر تلتزم بعدم بثّ محتوى مخالف للقانون أو الآداب أو حقوق الغير.
يحقّ لنا إيقاف أي بثّ مخالف وإزالة محتواه واتخاذ إجراء بحقّ الحساب.</p>

<h2>10. التقييمات والمراجعات</h2>
<p>يجب أن تكون تقييماتك صادقة ومبنيّة على تجربة فعلية. يُحظر التقييم المزيّف أو المُبتزّ أو المسيء،
ولنا إزالة ما يخالف ذلك.</p>

<h2>11. النزاعات والاسترجاع</h2>
<p>عند نشوء نزاع، يمكن فتحه عبر المنصّة التي قد تتدخّل كوسيط لتسهيل الحل بما في ذلك احتجاز المبالغ أو ردّها وفق سياساتنا.
قراراتنا في تسوية النزاعات ضمن آليّاتنا الداخلية تكون نهائية على مستوى المنصّة، دون المساس بحقّك في اللجوء للقضاء.</p>

<h2>12. السلوك المحظور</h2>
<ul>
  <li>الاحتيال أو الخداع أو انتحال الصفة أو تقديم بيانات كاذبة.</li>
  <li>التحايل على الرسوم أو نقل المعاملات خارج المنصّة تهرباً من التزاماتها.</li>
  <li>الإساءة أو التحرّش أو نشر محتوى غير قانوني أو مزعج (Spam).</li>
  <li>محاولة اختراق المنصّة أو الإخلال بأمنها أو استخدام وسائل آلية غير مصرّح بها.</li>
</ul>

<h2>13. الملكية الفكرية</h2>
<p>جميع حقوق المنصّة وعلاماتها وتصاميمها وبرمجياتها مملوكة لنا أو لمرخّصينا. وتمنحنا — بنشرك محتوى —
ترخيصاً غير حصري لعرضه واستخدامه لأغراض تشغيل المنصّة والترويج لها، مع بقاء ملكيتك لمحتواك.</p>

<h2>14. إخلاء الضمانات</h2>
<p>تُقدَّم المنصّة «كما هي» و«حسب توافرها» دون ضمانات صريحة أو ضمنية من أي نوع، بما في ذلك ملاءمتها لغرض معيّن أو خلوّها من الانقطاع أو الأخطاء.</p>

<h2>15. تحديد المسؤولية</h2>
<p>إلى الحدّ الذي يُجيزه القانون، لا نتحمّل مسؤولية أي أضرار غير مباشرة أو تبعية أو عرضية، ولا عن أفعال المستخدمين الآخرين أو جودة السلع.
ولا تتجاوز مسؤوليتنا الإجمالية — إن وُجدت — قيمة الرسوم التي دفعتها لنا خلال الأشهر الثلاثة السابقة للحادثة.</p>

<h2>16. التعويض</h2>
<p>توافق على تعويضنا والدفاع عنّا ضدّ أي مطالبات أو خسائر تنشأ عن مخالفتك هذه الشروط أو القانون أو حقوق الغير.</p>

<h2>17. التعليق والإنهاء</h2>
<p>يحقّ لنا تعليق أو إنهاء حسابك أو تقييد وصولك عند مخالفة هذه الشروط أو القانون أو حماية المنصّة ومستخدميها، مع مراعاة التزاماتك القائمة.</p>

<h2>18. القانون الحاكم وتسوية النزاعات</h2>
<p>تخضع هذه الشروط وتُفسَّر وفق قوانين <strong>جمهورية العراق</strong>، وتختصّ محاكم {c['city_ar']} بالنظر في أي نزاع ينشأ عنها،
دون إخلال بأي حقوق إلزامية يكفلها القانون للمستهلك.</p>

<h2>19. تعديل الشروط</h2>
<p>قد نُحدّث هذه الشروط من وقت لآخر. عند إجراء تغييرات جوهرية سنُشعرك داخل التطبيق وقد نطلب موافقتك من جديد قبل متابعة الاستخدام.</p>

<h2>20. أحكام عامة</h2>
<p>إذا سقط أي بند لعدم نفاذه، تبقى بقية البنود سارية. وعدم ممارستنا لحقّ ما لا يُعدّ تنازلاً عنه.
تمثّل هذه الشروط مع سياسة الخصوصية كامل الاتفاق بينك وبيننا بشأن استخدام المنصّة.</p>

<h2>21. تواصل معنا</h2>
<p>البريد الإلكتروني <a href="mailto:{c['email']}">{c['email']}</a> — الموقع <a href="{c['website']}">{c['website']}</a> — العنوان: {c['address_ar']}.</p>
"""
    return title, summary, html.strip()


# ============================================================================
# Privacy Policy — English
# ============================================================================
def privacy_en(c):
    title = 'Privacy Policy'
    summary = 'How Shorja collects, uses and protects your personal data.'
    html = f"""
<p>This Policy explains how the <strong>{c['brand_en']}</strong> application and platform ("we", "the Platform"),
operated by {c['entity_en']}, collects, uses, discloses and protects your personal data when you use our services.
By using the Platform you acknowledge that you have read and understood this Policy.</p>

<h2>1. Information We Collect</h2>
<h3>a) Information you provide</h3>
<ul>
  <li>Account data: username, email, phone number, and password (stored encrypted; we cannot read it).</li>
  <li>Store and listing data: item title, description, price, images, category and location.</li>
  <li>Communications: chat messages, ratings and reviews, and dispute details and attachments.</li>
  <li>Verification data we may request to confirm identity or store ownership when needed.</li>
</ul>
<h3>b) Information collected automatically</h3>
<ul>
  <li>Device data: type, operating system, device identifier and app language.</li>
  <li>Usage and logs: IP address, sections you visit, and access times.</li>
  <li>Push notification token (FCM) to send you alerts.</li>
</ul>
<h3>c) Location</h3>
<p>With your permission, we use your device location to show nearby listings and stores, determine your governorate, and facilitate delivery.
You may withdraw this permission at any time from your device settings.</p>
<h3>d) Financial and transaction data</h3>
<ul>
  <li>Wallet balance, deposit and withdrawal requests, and a history of transactions, auctions, bids and purchases.</li>
  <li>For payments/withdrawals we process only the last four digits of the card and a transaction reference;
      <strong>we do not store your full card number</strong> — payment data is handled by the secure payment provider.</li>
</ul>
<h3>e) Third-party sign-in</h3>
<p>If you sign in with Google or Facebook, we receive a unique identifier, your email and public name per your permissions there.</p>

<h2>2. How We Use Your Information</h2>
<ul>
  <li>Create and operate your account and enable you to list, buy, sell and bid.</li>
  <li>Process payments, the wallet, deposits, withdrawals, fees and commissions.</li>
  <li>Show relevant nearby content, listings and stores.</li>
  <li>Send operational notifications (messages, bids, order status) and security alerts.</li>
  <li>Prevent fraud and abuse, secure the Platform, and resolve disputes.</li>
  <li>Improve our services, analyse performance and fix crashes.</li>
  <li>Comply with legal and regulatory obligations.</li>
</ul>

<h2>3. Legal Bases for Processing</h2>
<p>We process your data based on: performance of our contract with you, your consent (e.g. location and marketing),
our legitimate interests (security, fraud prevention, service improvement), and compliance with legal obligations.</p>

<h2>4. Sharing of Information</h2>
<p>We do not sell your personal data. We may share it, only as necessary, with:</p>
<ul>
  <li><strong>Service providers:</strong> the payment processor (Stripe), Google Firebase services (notifications, analytics,
      crash reporting and authentication), the live-streaming provider (Agora), OpenStreetMap maps, delivery companies,
      and hosting/email providers — to operate the service on our behalf.</li>
  <li><strong>Other users:</strong> content you post publicly (listings, store name, reviews) is visible to others,
      and necessary contact information is shared with the counterparty upon a transaction or delivery.</li>
  <li><strong>Legal reasons:</strong> upon a lawful request from a competent authority in the Republic of Iraq, or to protect our rights and users.</li>
  <li><strong>Business transfers:</strong> in a merger, acquisition or asset sale, with your data remaining subject to this Policy.</li>
</ul>

<h2>5. Data Retention</h2>
<p>We retain your data while your account is active and for any additional period required by law, dispute resolution,
fraud prevention or financial record-keeping. When no longer needed, we delete or anonymise it.</p>

<h2>6. Data Security</h2>
<p>We apply reasonable technical and organisational measures including password hashing, short-lived access tokens (JWT),
and two-factor authentication for the admin panel. However, no method of transmission or storage is 100% secure and we cannot guarantee absolute security.</p>

<h2>7. Your Rights</h2>
<p>Subject to applicable law, you may access, correct or update your data, request deletion, withdraw consent, and object to certain processing.
To exercise these rights, contact us at the email below.</p>

<h2>8. Account Deletion</h2>
<p>You can delete your account in-app via "Settings". This removes or hides your personal data, except the minimum we must legally retain
(e.g. financial records or data relating to an open dispute).</p>

<h2>9. Children's Privacy</h2>
<p>Our services are intended for users aged <strong>18 and over</strong>. We do not knowingly collect data from anyone younger,
and will delete such data if collected without a lawful basis.</p>

<h2>10. Notifications and Marketing</h2>
<p>You can turn off notifications from your device or app settings. We may still send essential operational messages even if you opt out of marketing.</p>

<h2>11. International Transfers</h2>
<p>Your data may be processed or stored on our providers' servers that may be located outside the Republic of Iraq. We take reasonable steps to protect it under this Policy.</p>

<h2>12. Changes to this Policy</h2>
<p>We may update this Policy from time to time. For material changes we will notify you in-app and may ask you to accept again before continuing to use the service.</p>

<h2>13. Contact Us</h2>
<p>For any privacy inquiry: email <a href="mailto:{c['email']}">{c['email']}</a> —
website <a href="{c['website']}">{c['website']}</a> — address: {c['address_en']}.</p>
"""
    return title, summary, html.strip()


# ============================================================================
# Terms of Use — English
# ============================================================================
def terms_en(c):
    title = 'Terms of Use'
    summary = 'The rules governing your use of Shorja, and buying, selling and auctions on it.'
    html = f"""
<p>These Terms govern your use of the <strong>{c['brand_en']}</strong> application and platform operated by {c['entity_en']}.
By creating an account or using the Platform you agree to these Terms in full. If you do not agree, please do not use the Platform.</p>

<h2>1. Definitions</h2>
<p>"Platform" means the Shorja app and services. "User" is anyone who creates an account or uses the Platform.
"Seller" and "Buyer" are the parties to a transaction. "Store" is a seller's business profile. "Representative" is a party providing a
service on another's behalf via the Platform. "Auction" is a sale by public bidding.</p>

<h2>2. Eligibility</h2>
<p>You must be <strong>18 or older</strong> and have the legal capacity to contract. By using the Platform you confirm you meet this requirement.</p>

<h2>3. Account</h2>
<p>You agree to provide accurate, up-to-date information, keep your password confidential, and are responsible for all activity under your account.
Notify us immediately of any unauthorised use.</p>

<h2>4. Nature of the Platform</h2>
<p>Shorja is a technology intermediary enabling sellers to list items and buyers to transact with them. Contracts are formed directly between buyer and seller.
We do not own the goods and are not a party to the sale, except to the extent of specific services we provide (such as the wallet or holding funds until a transaction completes).
We do not warrant the quality of goods, the accuracy of descriptions, or the parties' ability to perform.</p>

<h2>5. Listings</h2>
<ul>
  <li>You warrant that you own the item or have the right to sell it, and that its description and images are accurate and not misleading.</li>
  <li>Listing anything prohibited under the law of the Republic of Iraq, or counterfeit, stolen, dangerous or contrary to public morals is forbidden
      (including, without limitation, weapons, drugs, prohibited materials and goods infringing others' rights).</li>
  <li>We may refuse, remove or edit any listing that violates these Terms without prior notice.</li>
</ul>

<h2>6. Auctions and Offers</h2>
<p>Placing a bid or a purchase offer is a serious and potentially binding commitment. If you win or your offer is accepted, you must complete the transaction on its terms.
Fees or security deposits may apply, as explained in the app.</p>

<h2>7. Payments, Wallet and Fees</h2>
<ul>
  <li>The wallet enables deposits, withdrawals and in-platform transactions; withdrawals are subject to administrative review and approval.</li>
  <li>We may charge commissions, service fees, storage or delivery fees, shown before you complete a transaction.</li>
  <li>You are responsible for any taxes or government charges due on your transactions. We may change fees in future with reasonable prior notice.</li>
</ul>

<h2>8. Representatives, Resale and Delivery</h2>
<p>The Platform may offer representative, resale and delivery services. Each party must perform its role honestly and per the agreed terms.
The Platform remains an organising intermediary for these services and does not guarantee their outcomes unless expressly stated.</p>

<h2>9. Live Streaming</h2>
<p>When using live streaming you agree not to broadcast content that is unlawful, immoral or infringing. We may stop any violating stream, remove its content and act against the account.</p>

<h2>10. Ratings and Reviews</h2>
<p>Your ratings must be honest and based on a real experience. Fake, coerced or abusive reviews are prohibited and may be removed.</p>

<h2>11. Disputes and Refunds</h2>
<p>When a dispute arises, it can be opened via the Platform, which may act as an intermediary to facilitate resolution, including holding or refunding funds under our policies.
Our decisions within our internal dispute mechanisms are final at the Platform level, without prejudice to your right to seek judicial recourse.</p>

<h2>12. Prohibited Conduct</h2>
<ul>
  <li>Fraud, deception, impersonation or providing false information.</li>
  <li>Circumventing fees or moving transactions off-Platform to evade obligations.</li>
  <li>Abuse, harassment, or posting unlawful or spam content.</li>
  <li>Attempting to hack the Platform, undermine its security, or use unauthorised automated means.</li>
</ul>

<h2>13. Intellectual Property</h2>
<p>All Platform rights, trademarks, designs and software are owned by us or our licensors. By posting content you grant us a non-exclusive licence to display and use it
to operate and promote the Platform, while you retain ownership of your content.</p>

<h2>14. Disclaimer of Warranties</h2>
<p>The Platform is provided "as is" and "as available" without warranties of any kind, express or implied, including fitness for a particular purpose or uninterrupted, error-free operation.</p>

<h2>15. Limitation of Liability</h2>
<p>To the extent permitted by law, we are not liable for indirect, consequential or incidental damages, nor for the acts of other users or the quality of goods.
Our total liability, if any, will not exceed the fees you paid us in the three months preceding the incident.</p>

<h2>16. Indemnity</h2>
<p>You agree to indemnify and defend us against any claims or losses arising from your breach of these Terms, the law, or others' rights.</p>

<h2>17. Suspension and Termination</h2>
<p>We may suspend or terminate your account or restrict your access upon violation of these Terms or the law, or to protect the Platform and its users, subject to your existing obligations.</p>

<h2>18. Governing Law and Dispute Resolution</h2>
<p>These Terms are governed by and construed under the laws of the <strong>Republic of Iraq</strong>, and the courts of {c['city_en']} have jurisdiction over any dispute arising from them,
without prejudice to mandatory consumer rights granted by law.</p>

<h2>19. Changes to the Terms</h2>
<p>We may update these Terms from time to time. For material changes we will notify you in-app and may ask you to accept again before continuing to use the service.</p>

<h2>20. General</h2>
<p>If any provision is held unenforceable, the remaining provisions stay in effect. Our failure to exercise a right is not a waiver of it.
These Terms together with the Privacy Policy constitute the entire agreement between you and us regarding use of the Platform.</p>

<h2>21. Contact Us</h2>
<p>Email <a href="mailto:{c['email']}">{c['email']}</a> — website <a href="{c['website']}">{c['website']}</a> — address: {c['address_en']}.</p>
"""
    return title, summary, html.strip()


DOCS = [
    (LegalDocument.DOC_PRIVACY, LegalDocument.LANG_AR, privacy_ar),
    (LegalDocument.DOC_PRIVACY, LegalDocument.LANG_EN, privacy_en),
    (LegalDocument.DOC_TERMS,   LegalDocument.LANG_AR, terms_ar),
    (LegalDocument.DOC_TERMS,   LegalDocument.LANG_EN, terms_en),
]


class Command(BaseCommand):
    help = "يزرع/يحدّث سياسة الخصوصية وشروط الاستخدام (عربي + إنجليزي)."

    def add_arguments(self, parser):
        parser.add_argument('--update', action='store_true',
                            help='تحديث محتوى أحدث إصدار موجود مكانه (بلا ترقيم جديد).')
        parser.add_argument('--bump', action='store_true',
                            help='إنشاء إصدار جديد (max+1) وإلزام المستخدمين بإعادة الموافقة.')
        parser.add_argument('--entity', help='الاسم القانوني للجهة المالكة.')
        parser.add_argument('--email', help='بريد التواصل.')
        parser.add_argument('--website', help='الموقع الإلكتروني.')
        parser.add_argument('--address', help='العنوان الكامل.')
        parser.add_argument('--city', help='مدينة الاختصاص القضائي.')

    def handle(self, *args, **options):
        c = _cfg(options)
        now = timezone.now()
        created = updated = bumped = skipped = 0

        for doc_type, lang, builder in DOCS:
            title, summary, content = builder(c)
            latest = (LegalDocument.objects
                      .filter(doc_type=doc_type, language=lang)
                      .order_by('-version').first())

            if latest is None:
                LegalDocument.objects.create(
                    doc_type=doc_type, language=lang, version=1,
                    title=title, summary=summary, content=content,
                    effective_at=now, is_published=True, requires_reacceptance=True,
                )
                created += 1
                self.stdout.write(f"  [+] {doc_type}/{lang} v1")
            elif options['bump']:
                LegalDocument.objects.create(
                    doc_type=doc_type, language=lang, version=latest.version + 1,
                    title=title, summary=summary, content=content,
                    effective_at=now, is_published=True, requires_reacceptance=True,
                )
                bumped += 1
                self.stdout.write(f"  [↑] {doc_type}/{lang} v{latest.version + 1}")
            elif options['update']:
                latest.title = title
                latest.summary = summary
                latest.content = content
                latest.save(update_fields=['title', 'summary', 'content', 'updated_at'])
                updated += 1
                self.stdout.write(f"  [~] {doc_type}/{lang} v{latest.version} (updated)")
            else:
                skipped += 1
                self.stdout.write(f"  [=] {doc_type}/{lang} v{latest.version} (exists — skipped)")

        self.stdout.write(self.style.SUCCESS(
            f"\nتم: created={created} updated={updated} bumped={bumped} skipped={skipped}"))
        if '«' in (c['entity_ar'] + c['address_ar'] + c['city_ar']):
            self.stdout.write(self.style.WARNING(
                "تنبيه: توجد عناصر نائبة «...» (الاسم القانوني/العنوان/مدينة الاختصاص). "
                "أعد التشغيل مع --entity/--address/--city ثم --update لملئها."))
