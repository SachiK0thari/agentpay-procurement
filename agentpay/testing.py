"""Test/eval harness: the whole system wired to fakes. No network."""
import copy
import json
from types import SimpleNamespace
from typing import Optional

from agentpay.approvals import ApprovalSigner
from agentpay.executor import Executor
from agentpay.models import LineItem, PurchaseIntent, Source
from agentpay.planner import CatalogEntry
from agentpay.policy import PolicyConfig
from agentpay.risk import RiskConfig
from agentpay.service import ProcurementService
from agentpay.store import Store, period_for
from agentpay.stripe_gateway import FakeStripe
from agentpay.webhooks import WebhookHandler, sign

WEBHOOK_SECRET = "whsec_test"
MCC = "computer_software_stores"
TRUSTED = {"vendor": Source.CATALOG, "quantity": Source.USER, "unit_amount": Source.CATALOG}

CATALOG = [
    CatalogEntry("Dell", "DOCK-1", "USB-C dock", 6_000, MCC),
    CatalogEntry("Dell", "MON-27", "27-inch monitor", 32_000, MCC),
    CatalogEntry("Dell", "LAPTOP-1", "Latitude laptop", 120_000, MCC),
    CatalogEntry("Figma", "SEAT-PRO", "Figma Professional seat (annual)", 1_500, MCC),
]


class Clock:
    def __init__(self, t: float = 1_792_500_000.0):  # Oct 2026
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_intent(amount=20_000, qty=1, vendor="Dell", user="alice", team="it", mcc=MCC, provenance=None,
                unit_amount=None, currency="usd", sku="DOCK-1"):
    if unit_amount is None:
        unit_amount = amount // qty if qty else amount
    return PurchaseIntent(
        user_id=user, team=team, vendor=vendor, mcc=mcc, currency=currency,
        items=[LineItem(sku=sku, description="USB-C dock", quantity=qty, unit_amount=unit_amount)],
        justification="new hires", provenance=dict(provenance or TRUSTED),
    )


class World:
    """Everything wired together with fakes. One per test."""

    def __init__(self, path=":memory:", budget=500_000):
        self.clock = Clock()
        self.store = Store(path, now=self.clock)
        self.store.set_budget("it", period_for(self.clock()), budget)
        self.stripe = FakeStripe()
        self.executor = Executor(self.store, self.stripe, sleep=lambda _: None)
        self.notified = []
        self.service = ProcurementService(
            store=self.store,
            policy_cfg=PolicyConfig(vendors={"Dell": {MCC}, "Figma": {"computer_software_stores"}}),
            risk_cfg=RiskConfig(trusted_vendors={"Dell"}),
            signer=ApprovalSigner(b"approval-secret"),
            executor=self.executor,
            approvers={"it": "bob"},
            now=self.clock,
            notifier=lambda *a: self.notified.append(a),
        )
        self.webhooks = WebhookHandler(self.store, self.stripe, WEBHOOK_SECRET, now=self.clock)
        self._n = 0

    def remaining_budget(self, team: str) -> Optional[int]:
        b = self.store.budget(team, period_for(self.clock()))
        return None if not b else b["limit_cents"] - b["spent_cents"] - b["reserved_cents"]

    def send(self, etype: str, obj: dict, event_id: str | None = None) -> dict:
        self._n += 1
        payload = json.dumps({"id": event_id or f"evt_{self._n}", "type": etype,
                              "data": {"object": obj}}).encode()
        return self.webhooks.handle(payload, sign(payload, WEBHOOK_SECRET, int(self.clock())))

    @staticmethod
    def auth(card_id, amount, name="DELL TECHNOLOGIES", category=MCC, approved=True, currency="usd"):
        """issuing_authorization.created shape: amount is the approved amount."""
        return {"id": "iauth_1", "card": {"id": card_id}, "amount": amount, "currency": currency,
                "approved": approved, "merchant_data": {"name": name, "category": category}}

    @staticmethod
    def auth_request(card_id, amount, name="DELL TECHNOLOGIES", category=MCC, currency="usd"):
        """issuing_authorization.request shape: amount is 0, the ask is in pending_request."""
        return {"id": "iauth_req_1", "card": {"id": card_id}, "amount": 0, "currency": currency,
                "approved": False, "status": "pending",
                "pending_request": {"amount": amount, "currency": currency, "is_amount_controllable": False,
                                    "merchant_amount": amount, "merchant_currency": currency},
                "merchant_data": {"name": name, "category": category}}

    @staticmethod
    def txn(card_id, amount, txn_id="ipi_1"):
        return {"id": txn_id, "card": card_id, "amount": -amount}

    def pay(self, card_id, amount):
        """Simulate the full happy-path webhook sequence (no merchant-side checks)."""
        self.send("issuing_authorization.created", self.auth(card_id, amount))
        self.send("issuing_transaction.created", self.txn(card_id, amount))

    def checkout(self, card_id, amount, *, name="DELL TECHNOLOGIES", category=MCC, currency="usd",
                 realtime=True, duplicate_webhooks=False, out_of_order=False, txn_id=None) -> tuple[bool, str]:
        """A merchant tries to charge the card, the way Stripe would sequence it:
        1. Stripe's own card controls (limit, category, status);
        2. our real-time issuing_authorization.request decision (can be switched off);
        3. if both approve: authorization.created + transaction.created webhooks, with the
           fault options the evals use. Returns (charged, layer_that_decided)."""
        ok, why = self.stripe.authorize(card_id, amount, currency, category)
        if not ok:
            return False, f"card_controls:{why}"
        if realtime:
            body = self.send("issuing_authorization.request",
                             self.auth_request(card_id, amount, name=name, category=category, currency=currency),
                             event_id=f"req:{card_id}:{amount}:{self._n}")
            if not body["approved"]:
                return False, "realtime_auth:" + ",".join(self.webhooks.last_decision[1])
        self.stripe.capture(card_id, amount)
        txn_id = txn_id or f"ipi_{card_id}_{self._n}"
        events = [("issuing_authorization.created", self.auth(card_id, amount, name=name, category=category, currency=currency)),
                  ("issuing_transaction.created", self.txn(card_id, amount, txn_id=txn_id))]
        if out_of_order:
            events.reverse()
        for etype, obj in events:
            for _ in range(2 if duplicate_webhooks else 1):
                self._deliver(etype, obj, event_id=f"{etype}:{txn_id}")
        return True, "charged"

    def _deliver(self, etype, obj, event_id):
        """Stripe retries a webhook whose handler failed; mirror that once."""
        try:
            self.send(etype, obj, event_id=event_id)
        except Exception:
            self.send(etype, obj, event_id=event_id)


# ---------------------------------------------------------------- scripted LLM
class FakeLLM:
    """Stand-in for `anthropic.Anthropic()`: scripted tool calls, then a proposal. No network.

    It plays the *model*, including a manipulated one: `proposal` is whatever the model ends
    up proposing after reading the (possibly injected) product page. The point of the evals
    is that the guardrails downstream hold regardless of what the model proposes.
    """

    def __init__(self, proposal: Optional[dict] = None,
                 tool_calls=(("search_catalog", {"query": "usb-c dock"}),
                             ("fetch_product_page", {"vendor": "Dell", "sku": "DOCK-1"}),
                             ("get_budget", {"team": "it"})),
                 final_stop_reason: str = "tool_use", tokens=(900, 120)):
        self.proposal = proposal
        self.tool_calls = list(tool_calls)
        self.final_stop_reason = final_stop_reason  # "refusal" / "end_turn" simulate failure modes
        self.tokens = tokens
        self.requests: list[dict] = []
        self.messages = self  # so `client.messages.create(...)` resolves

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})  # snapshot
        turn = len(self.requests) - 1
        usage = SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1],
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if turn < len(self.tool_calls):
            name, args = self.tool_calls[turn]
            block = SimpleNamespace(type="tool_use", id=f"toolu_{turn}", name=name, input=dict(args))
            return SimpleNamespace(stop_reason="tool_use", content=[block], usage=usage, stop_details=None)
        if self.final_stop_reason == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[], usage=usage,
                                   stop_details=SimpleNamespace(category="general_harms", explanation="declined"))
        if self.final_stop_reason != "tool_use" or self.proposal is None:
            text = SimpleNamespace(type="text", text="I cannot find a suitable item.")
            return SimpleNamespace(stop_reason=self.final_stop_reason, content=[text], usage=usage, stop_details=None)
        block = SimpleNamespace(type="tool_use", id="toolu_final", name="propose_purchase", input=dict(self.proposal))
        return SimpleNamespace(stop_reason="tool_use", content=[block], usage=usage, stop_details=None)
