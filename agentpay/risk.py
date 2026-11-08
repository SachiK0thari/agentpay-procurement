"""Risk check: explainable rules -> allow / review / block, with machine-readable reasons.

Optional "judges" (e.g. an LLM comparing the original request to the final intent) can
only ESCALATE. Nothing here can turn a review into an allow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .models import PurchaseIntent, RiskDecision, Source
from .store import IntentRecord

CRITICAL_FIELDS = ("vendor", "quantity", "unit_amount")

Judge = Callable[[PurchaseIntent], list[str]]  # returns reasons to escalate; [] = no concern


@dataclass
class RiskConfig:
    approval_threshold: int = 50_000
    near_threshold_band: float = 0.15         # within 15% below threshold
    split_window_s: int = 24 * 3600
    max_orders_per_hour: int = 10
    trusted_vendors: set[str] = field(default_factory=set)  # onboarded by finance; not "new"
    judges: Sequence[Judge] = field(default_factory=tuple)


@dataclass
class RiskResult:
    decision: RiskDecision
    reasons: list[str]


def assess(
    intent: PurchaseIntent,
    recent: list[IntentRecord],
    vendor_known: bool,
    cfg: RiskConfig,
    now: float,
) -> RiskResult:
    block: list[str] = []
    review: list[str] = []

    # 1. Provenance: a value the user never stated and the catalog doesn't confirm.
    for f in CRITICAL_FIELDS:
        if intent.provenance.get(f, Source.WEB) == Source.WEB:
            review.append(f"untrusted_provenance:{f}")

    # 2. Novelty.
    if not vendor_known:
        review.append("new_vendor")

    # 3. Velocity.
    last_hour = [r for r in recent if r.created_at >= now - 3600]
    if len(last_hour) >= cfg.max_orders_per_hour:
        block.append("velocity_exceeded")

    # 4. Split purchases: several sub-threshold orders to one vendor that add up past it.
    t = cfg.approval_threshold
    near = t * (1 - cfg.near_threshold_band) <= intent.amount < t
    window = [r for r in recent
              if r.intent.vendor == intent.vendor and r.created_at >= now - cfg.split_window_s]
    combined = intent.amount + sum(r.intent.amount for r in window)
    if window and intent.amount < t <= combined:
        review.append("possible_split_purchase")
    elif near:
        review.append("amount_near_threshold")

    # 5. Escalation-only judges.
    for judge in cfg.judges:
        review.extend(f"judge:{r}" for r in judge(intent))

    if block:
        return RiskResult(RiskDecision.BLOCK, block + review)
    if review:
        return RiskResult(RiskDecision.REVIEW, review)
    return RiskResult(RiskDecision.ALLOW, [])
