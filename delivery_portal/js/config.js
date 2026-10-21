// إعداد الاتصال بالـ API — عدّل القيمة عند النشر على سيرفر/دومين حقيقي.
// أثناء التطوير المحلي (تشغيل الباكند عبر manage.py runserver على نفس الجهاز):
//   API_BASE_URL = 'http://127.0.0.1:8000/api'
// بعد النشر:
//   API_BASE_URL = 'https://api.your-domain.com/api'
// تذكير مهم: يجب إضافة دومين هذه البوابة إلى CORS_ALLOWED_ORIGINS في
// balla/settings.py على الباكند وإلا سترفض المتصفح الطلبات (انظر README.md).
const CONFIG = {
  API_BASE_URL: 'https://api.shorjeh.com/api',
};
