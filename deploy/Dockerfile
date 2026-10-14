# صورة واحدة تخدم web و worker و beat — نفس الكود، أوامر مختلفة.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1

# mysqlclient يُبنى من المصدر ويحتاج مترجماً وترويسات libmysql.
# default-mysql-client يأتي معه عميل mysql — تستعمله نسخ mysqldump
# الاحتياطية من داخل الشبكة بدل فتح منفذ قاعدة البيانات للخارج.
RUN apt-get update && apt-get install -y --no-install-recommends         build-essential default-libmysqlclient-dev pkg-config         default-mysql-client curl     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# logs/ ضمن .gitignore فلا يصل مع الكود، وsettings.LOGGING يفتح
# logs/django.log عند الإقلاع — غيابه يُسقط Django قبل أن يبدأ، فتدور
# web/worker/beat في حلقة إعادة تشغيل ويردّ الوكيل 502.
#
# لا تعمل كـ root: ثغرة في Django تصبح صلاحية كاملة على الحاوية.
RUN useradd -m -u 1000 app && mkdir -p /app/media /app/staticfiles /app/logs     && chown -R app:app /app
USER app

EXPOSE 8000
