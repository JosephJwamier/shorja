"""The contract every payment provider implements.

Providers differ in almost everything — auth scheme, field names, how a hosted
page is reached, what a webhook carries — so the rest of the codebase is kept
away from all of it. Views and wallet code speak only in terms of this
interface plus the provider-agnostic `PaymentIntent` row.

Adding a second gateway is: write a class here, register it in registry.py,
add its credentials to settings. Nothing else in the project changes.

One rule is baked into the interface rather than left to each provider:
`fetch_status` exists and is authoritative. A webhook is treated as a *hint
that something happened*, never as proof of what happened — see
`webhooks.py` for why that matters with Al Qaseh specifically.
"""
from dataclasses import dataclass, field


class ProviderError(Exception):
    """The gateway refused, timed out, or answered with something unusable."""


@dataclass
class CreatedPayment:
    """What a provider hands back after a payment is opened.

    `checkout_url` is where the customer must be sent. Providers that take card
    data over the API instead of hosting a page leave it None; nothing in this
    project uses that path, because it would put the app inside PCI scope.
    """
    provider_payment_id: str
    provider_token: str = ""
    checkout_url: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PaymentStatus:
    """A provider's own status, mapped onto ours.

    `state` is one of PaymentIntent.STATE_* — providers translate their
    vocabulary into it so callers never switch on provider-specific strings.
    """
    state: str
    raw: dict = field(default_factory=dict)


class PaymentProvider:
    """Base class. `name` is what the app sends and what URLs route on."""

    name = ""
    #: Shown in the payment-method picker.
    label_ar = ""
    #: Currencies the provider settles in, upper case ISO codes.
    currencies = ("IQD",)

    @property
    def is_configured(self):
        """False when credentials are missing, so the method is hidden rather
        than offered and then failing at checkout."""
        raise NotImplementedError

    def create_payment(self, *, intent, redirect_url, webhook_url):
        """Open a payment at the gateway. Returns [CreatedPayment]."""
        raise NotImplementedError

    def fetch_status(self, *, intent):
        """Ask the gateway what actually happened. Returns [PaymentStatus].

        This is the authoritative read. It is called on webhook receipt and
        whenever the app polls, and its answer — not the webhook body — is what
        moves money.
        """
        raise NotImplementedError

    def parse_webhook(self, *, payload, headers):
        """Pull our own payment reference out of a webhook body.

        Returns the `order_id` the provider was given at creation time, or None
        when the payload is unrecognisable. Deliberately extracts nothing else:
        the values are unverified at this point and are only used to find the
        row that then gets re-fetched from the API.
        """
        raise NotImplementedError
