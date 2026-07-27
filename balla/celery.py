import os
from celery import Celery

# Set default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'balla.settings')

# Create Celery app
app = Celery('balla')

# Load config from Django settings with CELERY namespace
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()

# Explicitly load beat schedule from settings (ADD THESE TWO LINES)
from django.conf import settings
app.conf.beat_schedule = settings.CELERY_BEAT_SCHEDULE


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')