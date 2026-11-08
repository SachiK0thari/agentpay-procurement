"""Executor: the only code path that creates a payment credential. Never called by the LLM.

Idempotency in two layers:
  1. Locally: if the intent already has a card, return it.
  2. At Stripe: the idempotency key is derived from intent_id, so a crash between
     "Stripe created the card" and "we saved it" still yields exactly one card on retry.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .models import State
from .store import Store
from .stripe_gateway import StripeGateway, TransientStripeError


class NotApproved(Exception):
    pass


class Executor:
    def __init__(self, store: Store, gateway: StripeGateway, max_attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep,
                 crash_hook: Optional[Callable[[str], None]] = None):
        self.store = store
        self.gateway = gateway
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.crash_hook = crash_hook  # test-only fault injection

    def issue_card(self, intent_id: str) -> str:
        rec = self.store.get(intent_id)
        if rec.card_id:
            return rec.card_id
        if rec.state != State.APPROVED:
            raise NotApproved(f"{intent_id} is {rec.state.value}")

        key = f"{intent_id}:create_card"
        for attempt in range(self.max_attempts):
            try:
                card_id = self.gateway.create_card(
                    intent_id=intent_id, amount=rec.intent.amount, currency=rec.intent.currency,
                    mcc=rec.intent.mcc, idempotency_key=key,
                )
                break
            except TransientStripeError:
                self.sleep(min(0.2 * 2 ** attempt, 2.0))
        else:
            self.store.transition(intent_id, State.FAILED, ["stripe_unavailable"])
            self.store.release(intent_id)
            raise TransientStripeError(f"gave up after {self.max_attempts} attempts")

        if self.crash_hook:
            self.crash_hook("after_create_card")
        self.store.attach_card(intent_id, card_id)
        return card_id
