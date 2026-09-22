"""HTTP surface for payments.

Four endpoints, none of them provider-specific:

    GET  /api/payments/methods/          what the app offers in its picker
    POST /api/payments/create/           open a payment, get a checkout URL
    GET  /api/payments/{reference}/      poll after the customer comes back
    POST /api/payments/webhook/<name>/   gateway callback

The webhook is unauthenticated by necessity — gateways call it from their own
infrastructure. That is safe here only because it carries no authority: it is a
lookup key and a trigger, and the outcome is then read from the gateway over an
authenticated channel. See services/payments/service.py.
"""
import logging
from decimal import Decimal
from html import escape
from urllib.parse import quote

from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PaymentIntent
from .services.payments.base import ProviderError
from .services.payments.registry import enabled_providers, get_provider
from .services.payments.service import create_intent, sync_intent

logger = logging.getLogger(__name__)

# A floor and a ceiling, so a typo or a hostile client can't open a payment for
# 0.01 or for a billion.
MIN_AMOUNT = Decimal("1000")
MAX_AMOUNT = Decimal("5000000")


class PaymentIntentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentIntent
        fields = ["reference", "provider", "purpose", "amount", "currency",
                  "state", "checkout_url", "failure_reason", "created_at"]
        read_only_fields = fields


class CreatePaymentSerializer(serializers.Serializer):
    provider = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_amount(self, value):
        if value < MIN_AMOUNT or value > MAX_AMOUNT:
            raise serializers.ValidationError(
                "المبلغ يجب أن يكون بين %s و %s" % (MIN_AMOUNT, MAX_AMOUNT)
            )
        return value


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_methods(request):
    """Configured gateways only — a method the server can't actually use is
    never shown to the customer."""
    return Response({
        "success": True,
        "data": [
            {"provider": p.name, "label_ar": p.label_ar, "currencies": list(p.currencies)}
            for p in enabled_providers()
        ],
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_payment(request):
    serializer = CreatePaymentSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"success": False, "errors": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)

    try:
        intent = create_intent(
            user=request.user,
            provider_name=serializer.validated_data["provider"],
            amount=serializer.validated_data["amount"],
            request=request,
        )
    except ProviderError as exc:
        logger.warning("payment create failed for user %s: %s", request.user.id, exc)
        return Response(
            {"success": False, "message": "تعذّر بدء عملية الدفع، حاول مرة أخرى"},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response({"success": True, "data": PaymentIntentSerializer(intent).data},
                    status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def payment_detail(request, reference):
    """Polled by the app once the customer returns from the hosted page.

    Re-syncs while the payment is still open, so the app sees the settled state
    even if the webhook is slow or never arrives.
    """
    try:
        intent = PaymentIntent.objects.get(reference=reference, user=request.user)
    except PaymentIntent.DoesNotExist:
        return Response({"success": False, "message": "لا توجد عملية بهذا الرقم"},
                        status=status.HTTP_404_NOT_FOUND)

    if not intent.is_final:
        try:
            intent = sync_intent(intent)
        except ProviderError as exc:
            # The stored state is still returned: a gateway outage must not
            # look to the customer like a failed payment.
            logger.warning("status sync failed for %s: %s", reference, exc)

    return Response({"success": True, "data": PaymentIntentSerializer(intent).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sync_pending_payments(request):
    """Re-asks the gateway about this user's unfinished top-ups — the button
    behind the refresh arrow in the wallet.

    Manual on purpose: a customer who paid and closed the app before the
    return page would otherwise sit at 'pending' until something polled that
    intent. Bounded to the last 48 hours and a handful of intents, because
    each one is a live call to the gateway. A gateway outage on one intent
    leaves it pending and moves on; it is never reported as failed.
    """
    from .services.wallet_activity import pending_intents

    settled, still_pending, failed, errors = 0, 0, 0, 0
    for intent in list(pending_intents(request.user)[:10]):
        try:
            intent = sync_intent(intent)
        except ProviderError as exc:
            logger.warning("manual sync failed for %s: %s", intent.reference, exc)
            errors += 1
            continue
        if intent.state == PaymentIntent.STATE_SUCCEEDED:
            settled += 1
        elif intent.is_final:
            failed += 1
        else:
            still_pending += 1

    return Response({"success": True, "data": {
        "checked": settled + still_pending + failed + errors,
        "settled": settled,
        "still_pending": still_pending,
        "failed": failed,
        "errors": errors,
    }})


class PaymentWebhookView(APIView):
    """Gateway callback. Deliberately unauthenticated and deliberately dumb.

    It answers 200 to anything well-formed, because retrying a webhook we have
    already handled is worse for the gateway than a silent no-op is for us, and
    because a non-200 tells an attacker which references exist.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, provider_name):
        try:
            provider = get_provider(provider_name)
        except ProviderError:
            return Response({"success": False}, status=status.HTTP_404_NOT_FOUND)

        reference = provider.parse_webhook(payload=request.data,
                                           headers=request.headers)
        if not reference:
            logger.warning("unparseable %s webhook", provider_name)
            return Response({"success": True})

        intent = PaymentIntent.objects.filter(
            reference=reference, provider=provider.name
        ).first()
        if intent is None:
            logger.warning("%s webhook for unknown reference %r",
                           provider_name, reference)
            return Response({"success": True})

        try:
            sync_intent(intent)
        except ProviderError as exc:
            # Answer 200 anyway so the gateway stops retrying; the app's poll
            # and the reconciliation path both still resolve this payment.
            logger.error("webhook sync failed for %s: %s", reference, exc)

        return Response({"success": True})


RETURN_PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>العودة إلى التطبيق</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      background:#F9F9F9;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#1a1a1a}
 .card{text-align:center;padding:32px 24px;max-width:420px}
 h1{font-size:20px;margin:0 0 8px}
 p{color:#666;font-size:14px;line-height:1.7;margin:0 0 24px}
 a{display:block;background:#E4611A;color:#fff;text-decoration:none;padding:14px;
   border-radius:12px;font-weight:700;font-size:15px}
</style></head><body><div class="card">
<h1>انتهت عملية الدفع</h1>
<p>يمكنك العودة إلى التطبيق الآن. سيتحقّق التطبيق من نتيجة العملية تلقائياً.</p>
<a href="%(deep_link)s">العودة إلى التطبيق</a>
</div>
<script>location.replace(%(deep_link_json)s);</script>
</body></html>"""


@api_view(["GET"])
@permission_classes([AllowAny])
def payment_return(request):
    """Where the gateway sends the customer's browser after checkout.

    Carries no authority. The gateway appends `status` here as a query
    parameter, and query parameters are typed by whoever holds the keyboard —
    this page never reads it. The real outcome reaches the app by polling
    `payment_detail`, which re-asks the gateway.

    An HTML page rather than a 302 to the app's scheme, for two reasons.
    Django's `redirect()` refuses any scheme outside http/https/ftp, so a 302
    to `balla://` raises DisallowedRedirect. And a customer whose app was
    uninstalled mid-payment, or whose browser blocks scheme handoffs, would
    otherwise land on a browser error at the worst possible moment — right
    after paying. The script attempts the handoff, the button is there when it
    doesn't fire.
    """
    import json

    from django.conf import settings
    from django.http import HttpResponse

    scheme = getattr(settings, "APP_DEEP_LINK_SCHEME", "balla")
    order_id = request.GET.get("order_id", "")
    deep_link = "%s://payment-return?order_id=%s" % (scheme, quote(order_id, safe=""))

    return HttpResponse(RETURN_PAGE % {
        "deep_link": escape(deep_link),
        "deep_link_json": json.dumps(deep_link),
    })
