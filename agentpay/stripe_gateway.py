"""The only module that talks to Stripe. Everything else depends on the StripeGateway protocol,
so tests and evals run against FakeStripe with no network.

LiveStripe targets Stripe Issuing in TEST MODE. Verified against stripe-python 16.x:
`stripe.issuing.Card.create/modify`, error classes `stripe.APIConnectionError`,
`stripe.RateLimitError`, `stripe.APIError` (with `.http_status`).
"""
from __future__ import annotations

from typing import Optional, Protocol


class TransientStripeError(Exception):
    """Retryable: network error, 5xx, rate limit, or a lost response."""


class StripeGateway(Protocol):
    def create_card(self, *, intent_id: str, amount: int, currency: str, mcc: str,
                    idempotency_key: str) -> str: ...

    def cancel_card(self, card_id: str, *, idempotency_key: str) -> None: ...


class FakeStripe:
    """In-memory Stripe that honours idempotency keys the way the real API does:
    the same key returns the same object, even if the client never saw the first response.

    It also enforces the card's own spending controls (`authorize`), which the real Issuing
    API applies *before* it sends `issuing_authorization.request`. That lets the evals test
    the "card limit alone" layer with the real-time check switched off.
    """

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self._idem: dict[str, str] = {}
        self.fail_next = 0           # raise before doing anything
        self.lose_next_response = 0  # do the work, then raise (response lost in transit)
        self.cancel_fail_next = 0    # cancel_card raises (card stays active)

    def create_card(self, *, intent_id, amount, currency, mcc, idempotency_key) -> str:
        self.calls.append(("create_card", idempotency_key))
        if self.fail_next:
            self.fail_next -= 1
            raise TransientStripeError("simulated 500")
        if idempotency_key in self._idem:
            return self._idem[idempotency_key]
        card_id = f"ic_fake_{len(self.cards) + 1}"
        self.cards[card_id] = {
            "id": card_id, "status": "active", "currency": currency,
            "spending_limit": amount, "allowed_categories": [mcc], "spent": 0,
            "metadata": {"intent_id": intent_id},
        }
        self._idem[idempotency_key] = card_id
        if self.lose_next_response:
            self.lose_next_response -= 1
            raise TransientStripeError("simulated timeout after success")
        return card_id

    def cancel_card(self, card_id, *, idempotency_key) -> None:
        self.calls.append(("cancel_card", idempotency_key))
        if self.cancel_fail_next:
            self.cancel_fail_next -= 1
            raise TransientStripeError("simulated 500 on cancel")
        self.cards[card_id]["status"] = "canceled"

    # ---- Stripe-side card controls (what the network enforces, independent of our webhook)
    def authorize(self, card_id: str, amount: int, currency: str = "usd",
                  mcc: Optional[str] = None) -> tuple[bool, Optional[str]]:
        card = self.cards.get(card_id)
        if card is None:
            return False, "card_not_found"
        if card["status"] != "active":
            return False, f"card_{card['status']}"
        if currency != card["currency"]:
            return False, "currency_mismatch"
        if mcc is not None and mcc not in card["allowed_categories"]:
            return False, "category_not_allowed"
        if card["spent"] + amount > card["spending_limit"]:
            return False, "spending_limit_exceeded"
        return True, None

    def capture(self, card_id: str, amount: int) -> None:
        self.cards[card_id]["spent"] += amount


class LiveStripe:
    """Stripe Issuing, single-use virtual cards. Use a restricted test-mode key."""

    def __init__(self, api_key: str, cardholder_id: str):
        import stripe  # optional dependency

        if not api_key.startswith(("sk_test_", "rk_test_")):
            raise ValueError("LiveStripe refuses non-test keys in this project")
        stripe.api_key = api_key
        self._stripe = stripe
        self.cardholder_id = cardholder_id

    def create_card(self, *, intent_id, amount, currency, mcc, idempotency_key) -> str:
        with self._translate_errors():
            card = self._stripe.issuing.Card.create(
                cardholder=self.cardholder_id,
                currency=currency,
                type="virtual",
                status="active",
                spending_controls={
                    "spending_limits": [{"amount": amount, "interval": "all_time"}],
                    "allowed_categories": [mcc],
                },
                metadata={"intent_id": intent_id},
                idempotency_key=idempotency_key,
            )
        return card.id

    def cancel_card(self, card_id, *, idempotency_key) -> None:
        with self._translate_errors():
            self._stripe.issuing.Card.modify(card_id, status="canceled", idempotency_key=idempotency_key)

    def _translate_errors(self):
        return _ErrorTranslator(self._stripe)


class _ErrorTranslator:
    """Context manager: network errors, rate limits and 5xx become TransientStripeError.
    4xx (bad request, auth, card errors) propagate unchanged — retrying them can't help."""

    def __init__(self, stripe_mod):
        self.s = stripe_mod

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        s = self.s
        if isinstance(exc, (s.APIConnectionError, s.RateLimitError)):
            raise TransientStripeError(str(exc)) from exc
        if isinstance(exc, s.APIError) and (getattr(exc, "http_status", None) or 500) >= 500:
            raise TransientStripeError(f"stripe {exc.http_status}: {exc}") from exc
        return False
