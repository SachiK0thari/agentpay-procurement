"""Explicit lifecycle for a purchase. Every state change in the system goes through here.

DRAFT -> POLICY_CHECKED -> (PENDING_APPROVAL ->) APPROVED -> CARD_ISSUED
      -> AUTHORIZED -> CAPTURED -> RECONCILED
any non-terminal -> REJECTED | EXPIRED | FAILED (where listed)
"""
from __future__ import annotations

from .models import State

S = State

TRANSITIONS: dict[State, set[State]] = {
    S.DRAFT: {S.POLICY_CHECKED, S.REJECTED},
    S.POLICY_CHECKED: {S.PENDING_APPROVAL, S.APPROVED, S.REJECTED},
    S.PENDING_APPROVAL: {S.APPROVED, S.REJECTED, S.EXPIRED},
    S.APPROVED: {S.CARD_ISSUED, S.FAILED},
    # CARD_ISSUED -> CAPTURED is allowed because webhooks can arrive out of order
    # (transaction.created before authorization.created).
    S.CARD_ISSUED: {S.AUTHORIZED, S.CAPTURED, S.FAILED, S.EXPIRED},
    S.AUTHORIZED: {S.CAPTURED, S.FAILED},
    S.CAPTURED: {S.RECONCILED},
}

TERMINAL = {S.RECONCILED, S.REJECTED, S.EXPIRED, S.FAILED}

# Webhook-driven states only ever move forward along this ranking.
PAYMENT_RANK = {S.CARD_ISSUED: 0, S.AUTHORIZED: 1, S.CAPTURED: 2, S.RECONCILED: 3}


class IllegalTransition(Exception):
    pass


def check(src: State, dst: State) -> None:
    if dst not in TRANSITIONS.get(src, set()):
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed")
