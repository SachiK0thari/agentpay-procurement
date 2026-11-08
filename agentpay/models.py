"""Core data types. All money is integer minor units (cents) — never floats."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum


class State(str, Enum):
    DRAFT = "draft"
    POLICY_CHECKED = "policy_checked"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    CARD_ISSUED = "card_issued"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    RECONCILED = "reconciled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class Source(str, Enum):
    """Where a field's value came from. Assigned by code, never by the model."""
    USER = "user"        # stated by the requesting human
    CATALOG = "catalog"  # matched our trusted vendor catalog
    WEB = "web"          # only seen in untrusted content (pages, emails, tool output)


class RiskDecision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


@dataclass(frozen=True)
class LineItem:
    sku: str
    description: str
    quantity: int
    unit_amount: int  # cents


@dataclass
class PurchaseIntent:
    """What the planner proposes. Nothing here can move money on its own."""
    user_id: str
    team: str
    vendor: str
    mcc: str  # Stripe Issuing merchant category, e.g. "computer_software_stores"
    items: list[LineItem]
    currency: str = "usd"
    justification: str = ""
    provenance: dict[str, Source] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: "int_" + uuid.uuid4().hex[:16])

    @property
    def amount(self) -> int:
        return sum(i.quantity * i.unit_amount for i in self.items)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["provenance"] = {k: v.value for k, v in self.provenance.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PurchaseIntent":
        return cls(
            user_id=d["user_id"],
            team=d["team"],
            vendor=d["vendor"],
            mcc=d["mcc"],
            items=[LineItem(**i) for i in d["items"]],
            currency=d.get("currency", "usd"),
            justification=d.get("justification", ""),
            provenance={k: Source(v) for k, v in d.get("provenance", {}).items()},
            intent_id=d.get("intent_id") or "int_" + uuid.uuid4().hex[:16],
        )
