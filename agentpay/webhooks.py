"""Webhook handler: Stripe's events — not the agent — decide what happened.

- Signatures verified (Stripe-Signature scheme: t=<ts>,v1=<hmac(secret, "t.payload")>).
- Events deduped by id; handlers are idempotent anyway, so the id is recorded only after
  successful processing (a crash mid-handler means Stripe's retry is processed, not skipped).
- State moves forward only, so out-of-order and late events are harmless.
- issuing_authorization.request is the real-time check: approve only if the charge matches
  the approved intent.

Real-time authorization contract (confirmed against Stripe docs, Oct 2026):
  * the request event's top-level `amount` is 0; the amount being requested is in
    `pending_request.amount` / `pending_request.currency`;
  * the decision is returned in the HTTP response: status 200, a `Stripe-Version` header set
    to a specific API version, and JSON `{"approved": <bool>}` (optionally `amount` for
    partial approvals, only when `pending_request.is_amount_controllable`);
  * Stripe waits 2 seconds, then falls back to the account's timeout setting;
  * the separate /approve and /decline API calls are deprecated for this flow.
`process` returns exactly the body to send (plus the decision, for logging); the HTTP layer
adds the header. The real-time path fails closed: any error while deciding is a decline
(`decision_error`), never an exception. An exception would become a 5xx, and Stripe would then
apply the account's timeout fallback, which may be set to approve.

Transactions: `capture` converts the reservation into spend; `refund` is ledgered as a negative
amount and gives the money back to the budget; any other type is audited with no budget change.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import State
from .store import Store
from .stripe_gateway import StripeGateway


log = logging.getLogger("agentpay.webhooks")


class InvalidSignature(Exception):
    pass


@dataclass(frozen=True)
class WebhookResult:
    body: dict
    decision: Optional[tuple[bool, list[str]]] = None  # set for real-time authorization requests


def sign(payload: bytes, secret: str, ts: int) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def verify_signature(payload: bytes, header: str, secret: str, now: float, tolerance_s: int = 300) -> None:
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), None)
    sigs = [v for k, v in parts if k == "v1"]
    if ts is None or not sigs:
        raise InvalidSignature("malformed header")
    try:
        ts_int = int(ts)
    except ValueError:
        raise InvalidSignature("malformed timestamp")
    if abs(now - ts_int) > tolerance_s:
        raise InvalidSignature("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise InvalidSignature("signature mismatch")


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def merchant_matches(vendor: str, merchant_name: str) -> bool:
    """The descriptor must START with the vendor's name, as whole words.

    "DELL TECHNOLOGIES" / "Dell Technologies Inc." match Dell; "TOTALLY NOT FIGMA LLC" (vendor
    name inside someone else's descriptor) and "DELLA PIZZA" (prefix of a different word) don't.
    Processor prefixes such as "SQ *FIGMA" are declined too: a false block, the safe side.
    """
    v, m = _words(vendor), _words(merchant_name)
    return bool(v) and m[:len(v)] == v


def requested_amount(auth: dict) -> tuple[int, str]:
    """Amount/currency a merchant is asking for. On the request event it lives in
    pending_request; on created/updated events it's the top-level amount."""
    pending = auth.get("pending_request") or {}
    if "amount" in pending:
        return pending["amount"], pending.get("currency", auth.get("currency", "usd"))
    return auth["amount"], auth["currency"]


class WebhookHandler:
    def __init__(self, store: Store, gateway: StripeGateway, secret: str, now: Callable[[], float]):
        self.store = store
        self.gateway = gateway
        self.secret = secret
        self.now = now

    def handle(self, payload: bytes, sig_header: str) -> dict:
        return self.process(payload, sig_header).body

    def process(self, payload: bytes, sig_header: str) -> WebhookResult:
        """Verify, then handle. The decision is returned rather than stored on the handler, so
        concurrent requests can't log each other's decisions."""
        verify_signature(payload, sig_header, self.secret, self.now())
        event = json.loads(payload)
        etype = event.get("type") if isinstance(event, dict) else None

        if etype == "issuing_authorization.request":
            approved, reasons = self._decide_safely(event)
            return WebhookResult({"approved": approved}, (approved, reasons))  # exact body Stripe expects

        obj = event["data"]["object"]
        if self.store.event_seen(event["id"]):
            return WebhookResult({"status": "duplicate"})
        if etype == "issuing_authorization.created":
            self._on_authorization(obj)
        elif etype == "issuing_transaction.created":
            self._on_transaction(obj)
        self.store.mark_event(event["id"])
        return WebhookResult({"status": "ok"})

    def _decide_safely(self, event: dict) -> tuple[bool, list[str]]:
        auth: dict = {}
        try:
            auth = event["data"]["object"]
            approved, reasons = self.decide_authorization(auth)
        except Exception:
            log.exception("real-time authorization decision failed; declining")
            approved, reasons = False, ["decision_error"]
        try:
            rec = self.store.get_by_card(auth["card"]["id"])
            if rec is not None:
                self.store.note(rec.intent.intent_id,
                                [f"auth_{'approved' if approved else 'declined'}:{auth.get('id', '?')}"] + reasons)
        except Exception:
            # The audit note is best-effort; the decision must still reach Stripe in time.
            log.exception("could not audit real-time decision for %s", auth.get("id", "?"))
        return approved, reasons

    def decide_authorization(self, auth: dict) -> tuple[bool, list[str]]:
        rec = self.store.get_by_card(auth["card"]["id"])
        if rec is None:
            return False, ["unknown_card"]
        amount, currency = requested_amount(auth)
        reasons = []
        if rec.state != State.CARD_ISSUED:
            reasons.append(f"card_not_active_for_state:{rec.state.value}")
        if not isinstance(amount, int) or amount <= 0:
            reasons.append("non_positive_amount")
        elif amount > rec.intent.amount:
            reasons.append("amount_exceeds_intent")
        if currency != rec.intent.currency:
            reasons.append("currency_mismatch")
        m = auth["merchant_data"]
        if m["category"] != rec.intent.mcc:
            reasons.append("mcc_mismatch")
        if not merchant_matches(rec.intent.vendor, m["name"]):
            reasons.append("merchant_name_mismatch")
        return (not reasons), reasons

    def _on_authorization(self, auth: dict) -> None:
        rec = self.store.get_by_card(auth["card"]["id"])
        if rec and auth.get("approved"):
            self.store.advance(rec.intent.intent_id, State.AUTHORIZED)

    def _on_transaction(self, txn: dict) -> None:
        rec = self.store.get_by_card(txn["card"])
        if rec is None:
            return
        iid = rec.intent.intent_id
        kind = txn.get("type")
        if kind == "refund":
            # Refunds are positive (money coming back to the platform).
            recorded, credit = self.store.record_refund(iid, abs(txn["amount"]), ref=txn["id"])
            if recorded:
                reasons = [f"refund:{txn['id']}", f"budget_credited:{credit}"]
                if credit < abs(txn["amount"]):
                    reasons.append("refund_exceeds_capture")
                self.store.note(iid, reasons)
            return
        if kind != "capture":
            self.store.note(iid, [f"unhandled_transaction_type:{kind}:{txn.get('id', '?')}"])
            return
        # Issuing transactions are negative for purchases (money leaving the platform).
        amount = abs(txn["amount"])
        if self.store.record_capture(iid, amount, ref=txn["id"]) and amount > rec.intent.amount:
            # A force capture can exceed what was authorized; it's real money, so count it,
            # but make it visible.
            self.store.note(iid, [f"over_capture:{amount}>{rec.intent.amount}"])
        self.store.advance(iid, State.CAPTURED)
        # Single-use: kill the card so it can never be charged again, then reconcile.
        # If cancel raises, we return 5xx to Stripe and its retry re-runs this handler:
        # the ledger insert is a no-op, so no double count.
        self.gateway.cancel_card(rec.card_id, idempotency_key=f"{iid}:cancel_card")
        self.store.advance(iid, State.RECONCILED)
