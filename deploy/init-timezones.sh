#!/bin/bash
# يعمل مرة واحدة عند تهيئة حاوية MySQL لأول مرة.
#
# TIME_ZONE='Asia/Baghdad' يجعل Django يغلّف كل اقتطاع تاريخ واستعلام
# __date بـ CONVERT_TZ(col,'UTC','Asia/Baghdad'). تقرأ MySQL ذلك من جداول
# mysql.time_zone_* وهي فارغة في أي تثبيت جديد، فتُرجع NULL: إحصاءات
# المبيعات تصير صفراً وفلاتر التاريخ في لوحة الإدارة تعطي 500.
#
# core/checks.py (core.E001) يرفض migrate إن لم تُحمَّل — فهذا السطر ليس
# تحسيناً بل شرط إقلاع.
set -e
mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql --protocol=socket -u root -p"$MYSQL_ROOT_PASSWORD" mysql
echo "[init] MySQL time zone tables loaded"
