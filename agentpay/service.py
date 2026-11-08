"""Orchestration: PurchaseIntent in, decision out. Order of checks matters:

policy (pure) -> risk (rules + history) -> atomic budget reservation
  -> needs a human?  PENDING_APPROVAL + signed link
  -> otherwise       APPROVED -> Executor issues a single-use card
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import policy as policy_mod
from . import risk as risk_mod
from .approvals import ApprovalSigner, InvalidApproval
from .executor import Executor
from .models import PurchaseIntent, RiskDecision, State
from .store import Store
from .stripe_gateway import TransientStripeError

Notifier = Callable[[str, str, list[str]], None]  # (intent_id, approval_token, reasons)


@dataclass
class SubmitResult:
    intent_id: str
    state: State
    reasons: list[str] = field(default_factory=list)
    approval_token: Optional[str] = None
    card_id: Optional[str] = None


class ProcurementService:
    def __init__(self, store: Store, policy_cfg: policy_mod.PolicyConfig, risk_cfg: risk_mod.RiskConfig,
                 signer: ApprovalSigner, executor: Executor, approvers: dict[str, str],
                 now: Callable[[], float], notifier: Optional[Notifier] = None):
        self.store = store
        self.policy_cfg = policy_cfg
        self.risk_cfg = risk_cfg
        self.signer = signer
        self.executor = executor
        self.approvers = approvers  # team -> approver user id
        self.now = now
        self.notifier = notifier or (lambda *_: None)

    def submit(self, intent: PurchaseIntent) -> SubmitResult:
        now = self.now()
        recent = self.store.recent_for_user(intent.user_id, since=now - self.risk_cfg.split_window_s)
        known = (intent.vendor in self.risk_cfg.trusted_vendors
                 or self.store.vendor_known(intent.team, intent.vendor))
        self.store.create(intent)
        iid = intent.intent_id

        pol = policy_mod.evaluate(intent, self.policy_cfg)
        if not pol.allowed:
            return self._reject_draft(iid, pol.reasons)

        risk = risk_mod.assess(intent, recent, known, self.risk_cfg, now)
        if risk.decision == RiskDecision.BLOCK:
            return self._reject_draft(iid, risk.reasons)

        reasons = pol.reasons + risk.reasons
        self.store.transition(iid, State.POLICY_CHECKED, reasons)
        if not self.store.reserve(iid):
            self.store.transition(iid, State.REJECTED, ["budget_exhausted"])
            return SubmitResult(iid, State.REJECTED, ["budget_exhausted"])

        if pol.needs_approval or risk.decision == RiskDecision.REVIEW:
            approver = self.approvers[intent.team]
            token = self.signer.issue(iid, approver, now)
            self.store.transition(iid, State.PENDING_APPROVAL, reasons)
            self.notifier(iid, token, reasons)
            return SubmitResult(iid, State.PENDING_APPROVAL, reasons, approval_token=token)

        self.store.transition(iid, State.APPROVED, ["auto_approved"])
        card_id = self.executor.issue_card(iid)
        return SubmitResult(iid, self.store.get(iid).state, reasons, card_id=card_id)

    def approve(self, token: str, approver_id: str) -> SubmitResult:
        claim = self._claim(token, approver_id)
        if not self.store.transition(claim.intent_id, State.APPROVED, [f"approved_by:{approver_id}"]):
            raise InvalidApproval("state_changed")
        card_id = self.executor.issue_card(claim.intent_id)
        return SubmitResult(claim.intent_id, self.store.get(claim.intent_id).state, card_id=card_id)

    def reject(self, token: str, approver_id: str) -> SubmitResult:
        claim = self._claim(token, approver_id)
        self.store.transition(claim.intent_id, State.REJECTED, [f"rejected_by:{approver_id}"])
        self.store.release(claim.intent_id)
        return SubmitResult(claim.intent_id, State.REJECTED)

    def expire_stale(self) -> list[str]:
        """Run on a schedule. Unanswered approvals expire and give their budget back."""
        expired = []
        for iid in self.store.stale_pending(older_than=self.now() - self.signer.ttl_s):
            if self.store.transition(iid, State.EXPIRED, ["approval_timeout"]):
                self.store.release(iid)
                expired.append(iid)
        return expired

    def sweep_unused_cards(self, max_age_s: int = 48 * 3600) -> list[str]:
        """Run on a schedule. A card nobody charged (declined checkout, abandoned order) is a
        live credential; cancel it at Stripe, mark the intent EXPIRED, give the budget back.

        Cancel first, then flip state: if we crash in between, the next sweep re-cancels
        (idempotent) and finishes. The reverse order could leave a live card on an EXPIRED
        intent. A capture racing the sweep loses the compare-and-set and is recorded as usual.
        """
        swept = []
        for iid in self.store.stale_issued(older_than=self.now() - max_age_s):
            rec = self.store.get(iid)
            try:
                self.executor.gateway.cancel_card(rec.card_id, idempotency_key=f"{iid}:cancel_card")
            except TransientStripeError:
                continue  # still CARD_ISSUED; retried by the next sweep
            if self.store.transition(iid, State.EXPIRED, ["card_unused_timeout"]):
                self.store.release(iid)
                swept.append(iid)
        return swept

    def housekeeping(self, card_max_age_s: int = 48 * 3600) -> dict[str, list[str]]:
        return {"expired_approvals": self.expire_stale(), "swept_cards": self.sweep_unused_cards(card_max_age_s)}

    # ---------- internals ----------
    def _claim(self, token: str, approver_id: str):
        claim = self.signer.verify(token, self.now())
        rec = self.store.get(claim.intent_id)
        if claim.approver_id != approver_id:
            raise InvalidApproval("wrong_approver")
        if approver_id == rec.intent.user_id:
            raise InvalidApproval("self_approval")
        if rec.state != State.PENDING_APPROVAL:
            raise InvalidApproval(f"not_pending:{rec.state.value}")
        if not self.store.use_nonce(claim.nonce, claim.intent_id):
            raise InvalidApproval("replayed")
        return claim

    def _reject_draft(self, iid: str, reasons: list[str]) -> SubmitResult:
        self.store.transition(iid, State.REJECTED, reasons)
        return SubmitResult(iid, State.REJECTED, reasons)
