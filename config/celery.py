import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('playto')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'retry-stuck-payouts': {
        'task': 'payout.tasks.retry_stuck_payouts',
        'schedule': 30.0,
    },
}

app.conf.broker_url = os.environ.get('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
app.conf.result_backend = os.environ.get('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
