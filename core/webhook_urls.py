"""
ReSell Trust - Webhook URLs (core/webhook_urls.py)
Payment gateway webhooks
"""

from django.urls import path
from .webhook_views import (
    StripeWebhookView,
)

urlpatterns = [
    # Stripe webhook
    path('stripe/', StripeWebhookView.as_view(), name='stripe-webhook'),
]