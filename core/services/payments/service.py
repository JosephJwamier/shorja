"""Opening a payment, and settling one once the gateway confirms it.

The whole feature funnels through two functions here, so there is exactly one
place where money is created and exactly one rule about when it may be.

That rule: **money moves on `sync_intent`, never on a webhook body.** A webhook
only tells us to call `sync_intent`. See alqaseh.py for why — its signature
field is documented but its algorithm is not, so nothing in the payload can be
trusted. Re-asking the gateway over an authenticated channel costs one HTTP
call and removes the entire class of forged-callback fraud, including for
providers added later that might have no signature at all.
"""
import logging
import uuid

from django.db import transaction
from django.utils import timezone

from core.models import (
    DepositRequest, PaymentIntent, Transaction, get_or_create_wallet,
)

from .base import ProviderError
from .registry import get_provider

logger = logging.getLogger(__name__)


def _absolute(request, path):
    """Builds a URL the *gateway* can reach, which is not the same as one the
    app can reach.

    `build_absolute_uri` reflects whatever host the client used — in
    development that is `10.0.2.2:8000`, the Android emulator's alias for the
    host machine. Handing that to Al Qaseh as a webhook target means the
    callback is fired into a private address and silently never arrives.

    So a public base URL wins when configured (a tunnel in development, the
    real domain in production), and reflection is only the fallback for a
    server that is already publicly addressable.
    """
    from django.conf import settings as django_settings

    base = (getattr(django_settings, "PAYMENT_PUBLIC_BASE_URL", "") or "").rstrip("/")
    if base:
        return "%s%s" % (base, path)
    return request.build_absolute_uri(path)


def create_intent(*, user, provider_name, amount, request,
                  purpose=PaymentIntent.PURPOSE_WALLET_TOPUP, description=""):
    """Opens a payment at the gateway and returns the saved [PaymentIntent].

    The row is written before the gateway is called: if the call fails or the
    response is lost in transit, we still hold a record with the reference the
    gateway may have seen, and support can reconcile it. Creating the row
    afterwards would lose exactly the payments most worth investigating.
    """
    provider = get_provider(provider_name)
    if not provider.is_configured:
        raise ProviderError("payment provider %r is not configured" % provider_name)

    currency = provider.currencies[0]
    intent = PaymentIntent.objects.create(
        user=user,
        provider=provider.name,
        purpose=purpose,
        amount=amount,
        currency=currency,
        description=description or "شحن المحفظة",
        reference="PI-%s" % uuid.uuid4().hex[:20],
    )

    try:
        created = provider.create_payment(
            intent=intent,
            redirect_url=_absolute(request, "/api/payments/return/"),
            webhook_url=_absolute(request, "/api/payments/webhook/%s/" % provider.name),
        )
    except ProviderError:
        intent.state = PaymentIntent.STATE_FAILED
        intent.failure_reason = "gateway rejected the create call"
        intent.save(update_fields=["state", "failure_reason", "updated_at"])
        raise

    intent.provider_payment_id = created.provider_payment_id
    intent.provider_token = created.provider_token
    intent.checkout_url = created.checkout_url
    intent.last_provider_response = created.raw
    intent.save(update_fields=["provider_payment_id", "provider_token",
                               "checkout_url", "last_provider_response",
                               "updated_at"])
    return intent


def sync_intent(intent):
    """Asks the gateway what happened, and settles the intent if it succeeded.

    Safe to call repeatedly and concurrently — the webhook and the app polling
    on return routinely race. The row is locked and `applied_at` is re-checked
    inside the transaction, so the wallet is credited exactly once.

    Returns the refreshed [PaymentIntent].
    """
    if intent.applied_at is not None:
        return intent

    provider = get_provider(intent.provider)
    status = provider.fetch_status(intent=intent)

    with transaction.atomic():
        # Re-read under a lock: another worker may have settled this row
        # between the fetch above and here.
        locked = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
        locked.last_provider_response = status.raw
        locked.state = status.state

        if status.state != PaymentIntent.STATE_SUCCEEDED or locked.applied_at:
            locked.save(update_fields=["state", "last_provider_response", "updated_at"])
            return locked

        _apply(locked)

    return locked


def _apply(intent):
    """Credits what the payment bought. Caller holds the row lock."""
    if intent.purpose != PaymentIntent.PURPOSE_WALLET_TOPUP:
        # Unreachable today; guards the next purpose against silently
        # crediting a wallet it was never meant to touch.
        raise ProviderError("no handler for payment purpose %r" % intent.purpose)

    deposit = DepositRequest.objects.create(
        user=intent.user,
        amount=intent.amount,
        reference=intent.reference,
        status="completed",
        completed_at=timezone.now(),
    )
    wallet = get_or_create_wallet(intent.user)
    wallet.credit(intent.amount)

    Transaction.objects.create(
        user=intent.user,
        transaction_type="payment",
        amount=intent.amount,
        status="completed",
    )

    intent.deposit = deposit
    intent.applied_at = timezone.now()
    intent.save(update_fields=["deposit", "applied_at", "state",
                               "last_provider_response", "updated_at"])
    logger.info("payment %s settled: credited %s to %s",
                intent.reference, intent.amount, intent.user_id)
