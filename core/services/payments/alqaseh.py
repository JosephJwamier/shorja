"""Al Qaseh payment gateway.

Integrated on the PCI-DSS **Non-Certified** path: we never see, transmit or
store a card number. We create a payment context server-side, hand the customer
a hosted checkout URL, and learn the outcome from the API.

Docs: https://docs.alqaseh.com/ (v2.0)

Two things about that documentation shaped this file.

First, the webhook carries a `p_sign` field described as "the message
authentication code (MAC) used to verify the data integrity" — but the
algorithm and the key are documented nowhere. An unverifiable signature is not
a signature. So this provider never reads the outcome out of a webhook body;
`parse_webhook` extracts only the order id, and the real answer comes from
`fetch_status`, which is an authenticated call to the gateway. A forged webhook
can, at worst, make us ask Al Qaseh about a payment we already know about.

Second, the API types amounts as `float32`. Money is handled as `Decimal`
everywhere in this project, so the conversion is confined to the two edges
below and never leaks inward.
"""
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from .base import CreatedPayment, PaymentProvider, PaymentStatus, ProviderError

logger = logging.getLogger(__name__)

# Al Qaseh's transaction statuses, mapped onto ours. Anything absent from this
# table is treated as pending rather than guessed at — an unknown status must
# never fall through to "paid".
STATE_MAP = {
    "succeeded": "succeeded",
    "prepared": "pending",
    "retried": "pending",
    "unknown": "pending",
    "failed": "failed",
    "declined": "failed",
    "expired": "failed",
    "duplicated": "failed",
    "revoked": "cancelled",
}


class AlqasehProvider(PaymentProvider):
    name = "alqaseh"
    label_ar = "بطاقة (القاصة)"
    currencies = ("IQD",)

    def __init__(self, config=None):
        config = config or getattr(settings, "PAYMENT_PROVIDERS", {}).get(self.name, {})
        self.base_url = (config.get("BASE_URL") or "").rstrip("/")
        self.client_id = config.get("CLIENT_ID") or ""
        self.client_secret = config.get("CLIENT_SECRET") or ""
        self.checkout_base = (config.get("CHECKOUT_BASE_URL") or "").rstrip("/")
        self.timeout = config.get("TIMEOUT", 30)
        self.token_expiry_hours = config.get("TOKEN_EXPIRY_HOURS", 2)

    @property
    def is_configured(self):
        return bool(self.base_url and self.client_id and self.client_secret
                    and self.checkout_base)

    # -- HTTP ------------------------------------------------------------
    def _request(self, method, path, **kwargs):
        if not self.is_configured:
            raise ProviderError("Al Qaseh credentials are not configured")

        url = "%s%s" % (self.base_url, path)
        try:
            response = requests.request(
                method, url,
                auth=(self.client_id, self.client_secret),
                timeout=self.timeout,
                **kwargs
            )
        except requests.RequestException as exc:
            raise ProviderError("Al Qaseh unreachable: %s" % exc) from exc

        if response.status_code >= 400:
            # The body can carry the reason; the credentials never appear in it.
            raise ProviderError("Al Qaseh %s %s -> %s %s"
                                % (method, path, response.status_code,
                                   response.text[:300]))
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("Al Qaseh returned non-JSON for %s" % path) from exc

    # -- interface -------------------------------------------------------
    def create_payment(self, *, intent, redirect_url, webhook_url):
        body = {
            # float() only at this boundary — the column stays Decimal.
            "amount": float(intent.amount),
            "currency": intent.currency,
            "description": intent.description or "Wallet top-up",
            "order_id": intent.reference,
            "redirect_url": redirect_url,
            "webhook_url": webhook_url,
            "transaction_type": "Retail",
            "token_expiry_in_hour": self.token_expiry_hours,
        }
        if intent.user.email:
            body["email"] = intent.user.email[:80]

        data = self._request("POST", "/egw/payments/create", json=body)
        payment_id = str(data.get("payment_id") or "")
        token = str(data.get("token") or "")
        if not payment_id or not token:
            raise ProviderError("Al Qaseh create returned no payment_id/token: %r" % data)

        return CreatedPayment(
            provider_payment_id=payment_id,
            provider_token=token,
            checkout_url="%s/pay/%s" % (self.checkout_base, token),
            raw=data,
        )

    def fetch_status(self, *, intent):
        if not intent.provider_payment_id:
            raise ProviderError("intent %s has no provider payment id" % intent.pk)

        data = self._request("GET", "/egw/payments/%s" % intent.provider_payment_id)
        # The field name isn't pinned down in the docs prose, so accept the
        # spellings the API reference and the webhook section each use.
        raw_state = (data.get("payment_status") or data.get("status")
                     or data.get("state") or "")
        state = STATE_MAP.get(str(raw_state).strip().lower())
        if state is None:
            logger.warning("Al Qaseh returned unmapped status %r for intent %s",
                           raw_state, intent.pk)
            state = "pending"
        if state == "succeeded":
            self._check_settlement_matches(intent, data)
        return PaymentStatus(state=state, raw=data)

    @staticmethod
    def _check_settlement_matches(intent, data):
        """A 'succeeded' answer is accepted only for the amount, currency and
        order this intent was created with.

        The gateway fills all three from our own create call, so a mismatch is
        never the customer's doing — it is a partial capture, a currency slip
        or a status fetched for the wrong payment id — and crediting the
        intent's amount on it would be wrong money either way. Raising keeps
        the intent pending and puts the discrepancy in the logs for a human;
        fields the gateway leaves out are not checked."""
        problems = []
        if data.get("amount") is not None:
            try:
                paid = Decimal(str(data["amount"]))
            except (InvalidOperation, ValueError):
                paid = None
            if paid is None or paid != intent.amount:
                problems.append("amount %r != %s" % (data.get("amount"), intent.amount))
        currency = data.get("currency")
        if currency and str(currency).upper() != (intent.currency or "").upper():
            problems.append("currency %r != %s" % (currency, intent.currency))
        order_id = data.get("order_id")
        if order_id and str(order_id) != intent.reference:
            problems.append("order_id %r != %s" % (order_id, intent.reference))
        if problems:
            raise ProviderError("Al Qaseh settlement does not match intent %s: %s"
                                % (intent.reference, "; ".join(problems)))

    def parse_webhook(self, *, payload, headers):
        """Only the order id is taken, and it is treated as a lookup key.

        Everything else in the body is unverifiable (see the module docstring),
        so reading it would create the illusion of information we do not have.
        """
        if not isinstance(payload, dict):
            return None
        reference = payload.get("order_id")
        return str(reference) if reference else None
