"""The contract every installment financier implements.

Deliberately a mirror of services/payments/base.py, with one difference that
runs through everything: a payment gateway moves the *customer's* money and is
done in seconds, while a financier lends the customer money and then collects
from them for ten months. So this interface has two things the payment one
does not — an eligibility question asked before anything is created, and a
reversal, because an order can be cancelled while the financier is still
deducting from a salary.

Three rules are baked in rather than left to each provider:

1. **Eligibility is a question, not a side effect.** `check_eligibility`
   creates nothing at the financier. It exists so the buyer learns "your card
   can carry this" *before* the platform writes anything, and so the raw card
   number is sent exactly once (see cards.py).

2. **`fetch_plan` is authoritative.** A webhook is a hint that something
   happened, never proof of what happened — same reasoning as the payment
   webhooks, and worth more here: the payload claims money was collected.

3. **A refusal is not an error.** A financier saying "this card is not
   eligible" is a normal answer the buyer must see; a timeout or a malformed
   response is a fault. `NotEligible` versus `InstallmentError` keeps the two
   apart, so the app shows a reason instead of "something went wrong".
"""
from dataclasses import dataclass, field
from decimal import Decimal


class InstallmentError(Exception):
    """The financier refused the call, timed out, or answered unusably."""


class NotEligible(InstallmentError):
    """The financier answered, and the answer is no.

    Carries the Arabic reason to show the buyer. A subclass of
    [InstallmentError] so a caller that only cares "it didn't work" still
    catches it, while the view can answer 200-with-a-reason instead of 502.
    """

    def __init__(self, reason_ar='', raw=None):
        super().__init__(reason_ar or 'البطاقة غير مؤهّلة للتقسيط')
        self.reason_ar = reason_ar or 'البطاقة غير مؤهّلة للتقسيط'
        self.raw = raw or {}


@dataclass
class Eligibility:
    """The financier's answer about one card for one amount.

    `months` / `monthly_amount` / `customer_fee_percent` are what the
    financier itself says the schedule will be. They are None when it does not
    say, and then the platform's configured terms are used instead — see
    service.quote, which treats the financier's numbers as authoritative
    whenever they arrive, since it is the one collecting them.
    """

    eligible: bool
    card_holder_name: str = ""
    # Stands in for the card number in every later call, so the number itself
    # never has to be stored. Empty when the financier issues no token; the
    # plan then carries only last4 and the provider's own plan id.
    card_token: str = ""
    months: object = None
    monthly_amount: object = None
    customer_fee_percent: object = None
    # A ceiling the financier may impose on this specific card (remaining
    # salary capacity). Used to refuse early with a useful message.
    max_amount: object = None
    raw: dict = field(default_factory=dict)


@dataclass
class CreatedPlan:
    """What the financier hands back once the plan really exists."""

    provider_plan_id: str
    # The financier's own schedule, when it returns one. Overrides ours.
    months: object = None
    monthly_amount: object = None
    total_payable: object = None
    raw: dict = field(default_factory=dict)


@dataclass
class CollectedInstallment:
    """One deduction the financier reports. `sequence` is 1-based."""

    sequence: int
    amount: Decimal
    collected_at: object = None
    provider_reference: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PlanStatus:
    """A financier's view of a plan, mapped onto ours.

    `state` is one of InstallmentPlan.STATE_*. `collected` is the full list of
    deductions so far, not a delta — the platform diffs it against what it has
    recorded, so a replayed or out-of-order report cannot double-count.
    """

    state: str
    collected: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class Reversal:
    """The financier's answer to "stop collecting and take this money back"."""

    accepted: bool
    provider_reference: str = ""
    failure_reason: str = ""
    raw: dict = field(default_factory=dict)


class InstallmentProvider:
    """Base class. `name` is what the app sends and what URLs route on."""

    name = ""
    #: Shown in the payment picker.
    label_ar = ""
    currencies = ("IQD",)
    #: PAN lengths this financier issues, and whether its numbers carry a Luhn
    #: check digit. Local validation only — see cards.validate.
    card_lengths = (16,)
    require_luhn = True

    @property
    def is_configured(self):
        """False when credentials are missing, so installments are hidden
        rather than offered and then failing at checkout."""
        raise NotImplementedError

    # -- terms -----------------------------------------------------------
    # Read by service.quote to price a plan, and by the options endpoint to
    # tell the app what to show before any card is typed.

    @property
    def terms(self):
        """A [Terms] describing this financier's offer. Settings-driven, so
        renegotiating a percentage with the financier is an env change."""
        raise NotImplementedError

    # -- interface -------------------------------------------------------
    def check_eligibility(self, *, card_number, amount, user=None):
        """Ask whether this card can carry `amount`. Returns [Eligibility].

        Raises [NotEligible] for an answered refusal, [InstallmentError] for a
        fault. Must not create anything at the financier.
        """
        raise NotImplementedError

    def create_plan(self, *, plan, card_number=None):
        """Commit the plan at the financier. Returns [CreatedPlan].

        `card_number` is passed only when the financier issued no token at
        eligibility time; it is never persisted by the caller.
        """
        raise NotImplementedError

    def fetch_plan(self, *, plan):
        """Ask the financier what the plan's state and collections really are.
        Returns [PlanStatus]. This is the authoritative read."""
        raise NotImplementedError

    def reverse_plan(self, *, plan, amount, reason=""):
        """Stop collecting and return `amount` to the financier. Returns
        [Reversal]; a refusal comes back as `accepted=False`, not an
        exception, because the order is being cancelled either way and the
        platform has to record why the money could not follow it."""
        raise NotImplementedError

    def parse_webhook(self, *, payload, headers):
        """Pull our own plan reference out of a callback body.

        Returns the `reference` the financier was given at creation time, or
        None when the payload is unrecognisable. Extracts nothing else: the
        values are unverified at that point, and the real answer comes from
        `fetch_plan`.
        """
        raise NotImplementedError


@dataclass
class Terms:
    """What the platform offers, before any card is involved.

    `merchant_fee_percent` is zero in the monthly settlement mode by
    construction, not by configuration: there the financier takes nothing from
    the merchant because the platform, not the financier, is the one waiting
    ten months for the money. See QICARD_INSTALLMENTS_README.md.
    """

    months: int
    customer_fee_percent: Decimal
    merchant_fee_percent: Decimal
    settlement_mode: str
    min_amount: Decimal
    max_amount: Decimal
