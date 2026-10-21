"""Qi Card — salary-deduction installments.

What Qi Card offers (negotiated 2026-10), and how it maps onto this file:

  **upfront**  Qi pays the platform the whole price now, minus a merchant
               fee, and keeps deducting from the customer's salary for ten
               months. The customer's own fee is added on top of the price.
               Zero credit risk and zero financing cost for the platform —
               the default, and the only mode worth running for other
               people's goods. See QICARD_INSTALLMENTS_README.md for the
               arithmetic that settled it.

  **monthly**  Qi deducts monthly and deposits into the platform's own Qi
               account, taking only the customer's fee. No merchant fee, but
               the platform fronts the seller for ten months. Same code, one
               settings value: SETTLEMENT_MODE.

Two things about the integration shaped the code.

First, **the HTTP contract is not final.** Qi had not handed over an API
specification when this was written, so every request body and every response
field read here lives in the four `_map_*` helpers below and nowhere else, and
each response read accepts the spellings a bank API plausibly uses. Pinning it
to the real spec is editing those helpers — not the service, not the views,
not the app. Until then `SANDBOX` answers locally so the whole flow is
testable end to end; a sandbox provider is never `is_configured` in
production because it has no credentials.

Second, **a callback is never believed.** Qi's webhook says "installment 3
collected". Acting on that directly would let anyone who finds the URL inflate
what the platform thinks it has received, which in turn decides how much money
goes back to a buyer on a cancellation. So `parse_webhook` extracts a
reference and nothing else, and `fetch_plan` — an authenticated call — is what
the ledger is written from.
"""
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from .base import (
    CollectedInstallment, CreatedPlan, Eligibility, InstallmentError,
    InstallmentProvider, NotEligible, PlanStatus, Reversal, Terms,
)
from .cards import card_lengths_from, redact, require_luhn_from

logger = logging.getLogger(__name__)

# Qi's plan statuses mapped onto ours. Anything absent is treated as "no
# information" rather than guessed at — an unknown status must never fall
# through to `settled`, which is what stops the platform reversing a plan.
STATE_MAP = {
    "approved": "active",
    "active": "active",
    "in_progress": "active",
    "running": "active",
    "completed": "settled",
    "settled": "settled",
    "closed": "settled",
    "paid": "settled",
    "cancelled": "reversed",
    "canceled": "reversed",
    "reversed": "reversed",
    "refunded": "reversed",
    "rejected": "failed",
    "declined": "failed",
    "failed": "failed",
}

# Refusal codes Qi may answer with, as a sentence the buyer can act on.
# Anything unmapped falls back to the generic line — never the raw code, which
# means nothing to a customer.
REFUSAL_AR = {
    "insufficient_salary": "سقف الاستقطاع من راتبك لا يكفي لهذا المبلغ — "
                           "جرّب مبلغاً أقل",
    "card_inactive": "البطاقة غير مفعّلة — راجع فرع كي كارد",
    "card_not_found": "لم نجد هذه البطاقة — تأكّد من الرقم",
    "existing_plan": "لديك تقسيط جارٍ يمنع إضافة خطة جديدة",
    "blocked": "البطاقة محجوبة عن التقسيط",
    "limit_exceeded": "المبلغ يتجاوز سقف التقسيط المسموح لبطاقتك",
}


class QiCardProvider(InstallmentProvider):
    name = "qicard"
    label_ar = "تقسيط كي كارد"
    currencies = ("IQD",)

    def __init__(self, config=None):
        config = config if config is not None else getattr(
            settings, "INSTALLMENT_PROVIDERS", {}).get(self.name, {})
        self.config = config
        self.base_url = (config.get("BASE_URL") or "").rstrip("/")
        self.merchant_id = config.get("MERCHANT_ID") or ""
        self.client_id = config.get("CLIENT_ID") or ""
        self.client_secret = config.get("CLIENT_SECRET") or ""
        self.api_key = config.get("API_KEY") or ""
        self.webhook_secret = config.get("WEBHOOK_SECRET") or ""
        self.timeout = int(config.get("TIMEOUT", 30) or 30)
        self.sandbox = _flag(config.get("SANDBOX"))
        self.card_lengths = card_lengths_from(config)
        self.require_luhn = require_luhn_from(config)

    # -- configuration ---------------------------------------------------
    @property
    def is_configured(self):
        if self.sandbox:
            # Deliberate: the sandbox is for development and QA, and it must
            # be impossible to leave it switched on by accident in a
            # production build. DEBUG is the one switch nobody ships on.
            return bool(settings.DEBUG)
        return bool(self.base_url and self.merchant_id
                    and (self.api_key or (self.client_id and self.client_secret)))

    @property
    def terms(self):
        mode = (self.config.get("SETTLEMENT_MODE") or "upfront").strip().lower()
        if mode not in ("upfront", "monthly"):
            mode = "upfront"
        merchant_fee = _money(self.config.get("MERCHANT_FEE_PERCENT", "0"))
        if mode == "monthly":
            # Not a configuration choice: in this mode Qi collects only the
            # customer's fee, because the platform — not Qi — is the one
            # carrying the money for ten months.
            merchant_fee = Decimal("0.00")
        return Terms(
            months=int(self.config.get("MONTHS", 10) or 10),
            customer_fee_percent=_money(self.config.get("CUSTOMER_FEE_PERCENT", "0")),
            merchant_fee_percent=merchant_fee,
            settlement_mode=mode,
            min_amount=_money(self.config.get("MIN_AMOUNT", "0")),
            max_amount=_money(self.config.get("MAX_AMOUNT", "0")),
        )

    # -- HTTP ------------------------------------------------------------
    def _request(self, method, path, **kwargs):
        if not self.base_url:
            raise InstallmentError("Qi Card base URL is not configured")

        headers = {"Accept": "application/json"}
        auth = None
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        else:
            auth = (self.client_id, self.client_secret)

        try:
            response = requests.request(
                method, "%s%s" % (self.base_url, path),
                auth=auth, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise InstallmentError("Qi Card unreachable: %s" % exc) from exc

        if response.status_code >= 400:
            body = response.text[:300]
            # A 4xx can be a refusal rather than a fault — Qi answers both
            # ways depending on the endpoint, so look for a refusal code
            # before calling it an error.
            code, reason = _refusal_from(_json_or_none(response))
            if code:
                raise NotEligible(reason, raw={"status": response.status_code,
                                               "code": code})
            raise InstallmentError("Qi Card %s %s -> %s %s"
                                   % (method, path, response.status_code, body))
        data = _json_or_none(response)
        if data is None:
            raise InstallmentError("Qi Card returned non-JSON for %s" % path)
        return data

    # -- interface -------------------------------------------------------
    def check_eligibility(self, *, card_number, amount, user=None):
        if self.sandbox:
            return self._sandbox_eligibility(card_number, amount)

        data = self._request("POST", "/installments/eligibility", json={
            "merchant_id": self.merchant_id,
            "card_number": card_number,
            "amount": float(amount),
            "currency": "IQD",
            "months": self.terms.months,
        })
        return self._map_eligibility(data, amount)

    def create_plan(self, *, plan, card_number=None):
        if self.sandbox:
            return self._sandbox_create(plan)

        body = {
            "merchant_id": self.merchant_id,
            "order_id": plan.reference,
            "amount": float(plan.principal),
            "currency": "IQD",
            "months": plan.months,
            "settlement_mode": plan.settlement_mode,
        }
        if plan.card_token:
            body["card_token"] = plan.card_token
        elif card_number:
            body["card_number"] = card_number
        else:
            raise InstallmentError("plan %s has neither a card token nor a number"
                                   % plan.reference)

        data = self._request("POST", "/installments/plans", json=body)
        return self._map_created(data, plan)

    def fetch_plan(self, *, plan):
        if self.sandbox:
            return self._sandbox_status(plan)
        if not plan.provider_plan_id:
            raise InstallmentError("plan %s has no provider id" % plan.reference)
        data = self._request("GET", "/installments/plans/%s" % plan.provider_plan_id)
        return self._map_status(data, plan)

    def reverse_plan(self, *, plan, amount, reason=""):
        if self.sandbox:
            return Reversal(accepted=True,
                            provider_reference="SBX-REV-%s" % plan.reference,
                            raw={"sandbox": True, "amount": str(amount)})
        if not plan.provider_plan_id:
            return Reversal(accepted=False,
                            failure_reason="no provider plan id on record")
        try:
            data = self._request(
                "POST", "/installments/plans/%s/reverse" % plan.provider_plan_id,
                json={"amount": float(amount), "reason": reason[:200],
                      "merchant_id": self.merchant_id},
            )
        except InstallmentError as exc:
            # Never raised onward: the order is being cancelled either way,
            # and the platform has to record that the money could not follow
            # it. See service.reverse, which parks it for a human.
            return Reversal(accepted=False, failure_reason=str(exc)[:200])
        return self._map_reversal(data)

    def parse_webhook(self, *, payload, headers):
        if not isinstance(payload, dict):
            return None
        for key in ("order_id", "reference", "merchant_order_id", "plan_reference"):
            value = payload.get(key)
            if value:
                return str(value)
        nested = payload.get("data") or payload.get("plan")
        if isinstance(nested, dict):
            return self.parse_webhook(payload=nested, headers=headers)
        return None

    def webhook_secret_ok(self, provided):
        """A cheap filter, not an authorisation.

        The callback carries no authority here — the outcome is re-fetched
        over an authenticated channel — so this only keeps noise out of the
        logs and the task queue. Configured secret absent means accept, for
        the same reason the payment webhook does.
        """
        if not self.webhook_secret:
            return True
        return _constant_time_equals(str(provided or ""), self.webhook_secret)

    # -- response mapping (edit here when Qi pins the contract) ----------
    def _map_eligibility(self, data, amount):
        node = data.get("data") if isinstance(data.get("data"), dict) else data
        code, reason = _refusal_from(data)
        eligible = node.get("eligible")
        if eligible is None:
            eligible = str(node.get("status", "")).strip().lower() in (
                "eligible", "approved", "ok", "true")
        if not eligible:
            raise NotEligible(reason or REFUSAL_AR.get(code or "", ""), raw=data)

        return Eligibility(
            eligible=True,
            card_holder_name=str(node.get("holder_name")
                                 or node.get("card_holder_name") or "")[:120],
            card_token=str(node.get("card_token") or node.get("token") or ""),
            months=_int_or_none(node.get("months") or node.get("tenor")),
            monthly_amount=_money_or_none(node.get("monthly_amount")
                                          or node.get("installment_amount")),
            customer_fee_percent=_money_or_none(node.get("fee_percent")
                                                or node.get("customer_fee_percent")),
            max_amount=_money_or_none(node.get("max_amount")
                                      or node.get("available_limit")),
            raw=data,
        )

    def _map_created(self, data, plan):
        node = data.get("data") if isinstance(data.get("data"), dict) else data
        plan_id = str(node.get("plan_id") or node.get("id")
                      or node.get("installment_id") or "")
        if not plan_id:
            raise InstallmentError("Qi Card create returned no plan id: %r" % data)

        raw_state = str(node.get("status") or node.get("state") or "").strip().lower()
        if raw_state and STATE_MAP.get(raw_state) == "failed":
            code, reason = _refusal_from(data)
            raise NotEligible(reason or REFUSAL_AR.get(code or "", ""), raw=data)

        return CreatedPlan(
            provider_plan_id=plan_id,
            months=_int_or_none(node.get("months") or node.get("tenor")),
            monthly_amount=_money_or_none(node.get("monthly_amount")
                                          or node.get("installment_amount")),
            total_payable=_money_or_none(node.get("total_payable")
                                         or node.get("total_amount")),
            raw=data,
        )

    def _map_status(self, data, plan):
        node = data.get("data") if isinstance(data.get("data"), dict) else data
        raw_state = str(node.get("status") or node.get("state") or "").strip().lower()
        state = STATE_MAP.get(raw_state)
        if state is None:
            logger.warning("Qi Card returned unmapped plan status %r for %s",
                           raw_state, plan.reference)
            state = plan.state if plan.state != plan.STATE_QUOTED else plan.STATE_ACTIVE

        rows = (node.get("installments") or node.get("collections")
                or node.get("schedule") or [])
        collected = []
        for index, row in enumerate(rows if isinstance(rows, list) else [], start=1):
            if not isinstance(row, dict):
                continue
            paid = row.get("collected") if row.get("collected") is not None else (
                str(row.get("status", "")).strip().lower() in ("collected", "paid", "settled"))
            if not paid:
                continue
            when = row.get("collected_at") or row.get("paid_at") or row.get("date")
            collected.append(CollectedInstallment(
                sequence=_int_or_none(row.get("sequence") or row.get("number")) or index,
                amount=_money(row.get("amount") or 0),
                collected_at=_parse_date(when),
                provider_reference=str(row.get("reference") or row.get("id") or "")[:128],
                raw=row,
            ))
        return PlanStatus(state=state, collected=collected, raw=data)

    def _map_reversal(self, data):
        node = data.get("data") if isinstance(data.get("data"), dict) else data
        accepted = node.get("accepted")
        if accepted is None:
            accepted = str(node.get("status", "")).strip().lower() in (
                "accepted", "reversed", "cancelled", "canceled", "ok", "success")
        return Reversal(
            accepted=bool(accepted),
            provider_reference=str(node.get("reference") or node.get("id") or "")[:128],
            failure_reason="" if accepted else str(
                node.get("message") or node.get("error") or "")[:200],
            raw=data,
        )

    # -- sandbox ---------------------------------------------------------
    # Deterministic answers keyed off the card's last digit, so a tester can
    # reach every branch without Qi: 0 refuses, 9 caps the limit low, and
    # anything else approves. Documented in QICARD_INSTALLMENTS_README.md.
    def _sandbox_eligibility(self, card_number, amount):
        tail = card_number[-1]
        if tail == "0":
            raise NotEligible("سقف الاستقطاع من راتبك لا يكفي لهذا المبلغ (بطاقة اختبار)",
                              raw={"sandbox": True})
        terms = self.terms
        cap = Decimal("500000") if tail == "9" else terms.max_amount
        if cap and Decimal(amount) > cap:
            raise NotEligible("المبلغ يتجاوز سقف التقسيط المسموح لبطاقتك (بطاقة اختبار)",
                              raw={"sandbox": True, "max_amount": str(cap)})
        return Eligibility(
            eligible=True,
            card_holder_name="حامل بطاقة الاختبار",
            card_token="sbx_%s" % hashlib.sha256(card_number.encode()).hexdigest()[:24],
            months=terms.months,
            max_amount=cap or None,
            raw={"sandbox": True, "card": redact(card_number)},
        )

    def _sandbox_create(self, plan):
        return CreatedPlan(
            provider_plan_id="SBX-%s" % plan.reference,
            months=plan.months,
            raw={"sandbox": True},
        )

    def _sandbox_status(self, plan):
        """Pretends one installment has been collected per elapsed month, so
        the collection and reversal paths can be exercised by moving the
        plan's dates rather than waiting."""
        months_elapsed = 0
        if plan.applied_at:
            months_elapsed = int((timezone.now() - plan.applied_at).days // 30)
        months_elapsed = max(0, min(months_elapsed, plan.months))
        collected = [
            CollectedInstallment(
                sequence=i,
                amount=plan.monthly_amount,
                collected_at=plan.applied_at + timedelta(days=30 * i),
                provider_reference="SBX-C%d-%s" % (i, plan.reference),
                raw={"sandbox": True},
            )
            for i in range(1, months_elapsed + 1)
        ]
        state = plan.STATE_SETTLED if months_elapsed >= plan.months else plan.STATE_ACTIVE
        return PlanStatus(state=state, collected=collected, raw={"sandbox": True})


# -- small helpers -----------------------------------------------------------

def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _money(value):
    return Decimal(str(value or "0"))


def _money_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


def _parse_date(value):
    if not value:
        return timezone.now()
    from django.utils.dateparse import parse_datetime, parse_date
    text = str(value)
    parsed = parse_datetime(text)
    if parsed is None:
        day = parse_date(text[:10])
        if day is None:
            return timezone.now()
        parsed = timezone.datetime(day.year, day.month, day.day)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _refusal_from(data):
    """(code, arabic reason) out of whatever shape the refusal arrived in."""
    if not isinstance(data, dict):
        return None, ""
    node = data.get("data") if isinstance(data.get("data"), dict) else data
    code = (node.get("reason_code") or node.get("code") or node.get("error_code")
            or node.get("error") or "")
    code = str(code).strip().lower() or None
    reason = REFUSAL_AR.get(code or "", "")
    return code, reason


def _constant_time_equals(a, b):
    from hmac import compare_digest
    return compare_digest(a, b)
