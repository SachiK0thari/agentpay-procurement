"""Policy engine: pure functions over a PurchaseIntent. No I/O, no LLM, fully unit-testable.

Budget is NOT checked here — it needs an atomic reservation (see Store.reserve), so a
pure read-then-decide check would race.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import PurchaseIntent


@dataclass
class PolicyConfig:
    per_txn_limit: int = 200_000          # $2,000 hard cap per order
    approval_threshold: int = 50_000      # orders >= $500 need a human
    vendors: dict[str, set[str]] = field(default_factory=dict)  # vendor -> allowed MCCs
    currencies: set[str] = field(default_factory=lambda: {"usd"})
    max_quantity_per_line: int = 25


@dataclass
class PolicyDecision:
    allowed: bool
    needs_approval: bool
    reasons: list[str]


def evaluate(intent: PurchaseIntent, cfg: PolicyConfig) -> PolicyDecision:
    hard: list[str] = []
    soft: list[str] = []

    if not intent.items:
        hard.append("no_items")
    for item in intent.items:
        if item.quantity <= 0 or item.unit_amount <= 0:
            hard.append(f"invalid_line:{item.sku}")
        if item.quantity > cfg.max_quantity_per_line:
            hard.append(f"quantity_over_limit:{item.sku}")

    if intent.currency not in cfg.currencies:
        hard.append(f"currency_not_allowed:{intent.currency}")
    if intent.vendor not in cfg.vendors:
        hard.append("vendor_not_allowlisted")
    elif intent.mcc not in cfg.vendors[intent.vendor]:
        hard.append("mcc_not_allowed_for_vendor")

    if intent.amount > cfg.per_txn_limit:
        hard.append("over_per_txn_limit")
    elif intent.amount >= cfg.approval_threshold:
        soft.append("over_approval_threshold")

    return PolicyDecision(allowed=not hard, needs_approval=bool(soft), reasons=hard + soft)
