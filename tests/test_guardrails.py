"""One test group per guardrail. Each tries to defeat a layer on its own."""
import threading

import pytest

from agentpay.approvals import InvalidApproval
from agentpay.models import Source, State
from agentpay.state_machine import IllegalTransition, check
from agentpay.store import Store, period_for
from agentpay.webhooks import InvalidSignature

from agentpay.testing import TRUSTED, World, make_intent


# ---------------------------------------------------------------- state machine
def test_happy_path_reaches_reconciled(world):
    r = world.service.submit(make_intent(20_000))
    assert r.state == State.CARD_ISSUED and r.card_id
    world.pay(r.card_id, 20_000)
    assert world.store.get(r.intent_id).state == State.RECONCILED
    assert world.stripe.cards[r.card_id]["status"] == "canceled"
    assert world.store.budget("it", period_for(world.clock()))["spent_cents"] == 20_000


def test_illegal_transition_is_rejected():
    with pytest.raises(IllegalTransition):
        check(State.DRAFT, State.CARD_ISSUED)
    with pytest.raises(IllegalTransition):
        check(State.RECONCILED, State.CAPTURED)


# ---------------------------------------------------------------- 1. spending limits
def test_over_per_txn_limit_is_rejected(world):
    r = world.service.submit(make_intent(200_001))
    assert r.state == State.REJECTED and "over_per_txn_limit" in r.reasons
    assert world.stripe.cards == {}


def test_vendor_not_allowlisted(world):
    r = world.service.submit(make_intent(10_000, vendor="ShadyParts"))
    assert r.state == State.REJECTED and "vendor_not_allowlisted" in r.reasons


def test_budget_exhausted():
    w = World(budget=30_000)
    assert w.service.submit(make_intent(20_000)).state == State.CARD_ISSUED
    r = w.service.submit(make_intent(20_000, user="carol"))
    assert r.state == State.REJECTED and r.reasons == ["budget_exhausted"]


def test_card_limit_equals_approved_amount(world):
    r = world.service.submit(make_intent(20_000))
    card = world.stripe.cards[r.card_id]
    assert card["spending_limit"] == 20_000
    assert card["allowed_categories"] == ["computer_software_stores"]


def test_concurrent_reservations_cannot_overspend(tmp_path):
    """Two workers race for the last $500. Exactly one wins."""
    path = str(tmp_path / "race.db")
    w = World(path=path, budget=50_000)
    a, b = make_intent(30_000), make_intent(30_000, user="carol")
    w.store.create(a)
    w.store.create(b)
    results, barrier = {}, threading.Barrier(2)

    def worker(intent):
        s = Store(path, now=w.clock)  # separate connection, like a separate process
        barrier.wait()
        results[intent.intent_id] = s.reserve(intent.intent_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == [False, True]
    assert w.store.budget("it", period_for(w.clock()))["reserved_cents"] == 30_000


# ---------------------------------------------------------------- 2. manipulation / risk
def test_web_sourced_quantity_goes_to_review(world):
    prov = dict(TRUSTED, quantity=Source.WEB)  # e.g. "buy 50" came from a product page
    r = world.service.submit(make_intent(20_000, provenance=prov))
    assert r.state == State.PENDING_APPROVAL
    assert "untrusted_provenance:quantity" in r.reasons
    assert world.stripe.cards == {}


def test_split_purchase_is_caught(world):
    for i in range(3):
        r = world.service.submit(make_intent(30_000 + i))  # each well under $500
    assert r.state == State.PENDING_APPROVAL and "possible_split_purchase" in r.reasons


def test_new_vendor_needs_review(world):
    r = world.service.submit(make_intent(5_000, vendor="Figma"))
    assert r.state == State.PENDING_APPROVAL and "new_vendor" in r.reasons


def test_judge_can_escalate_but_never_approve(world):
    world.service.risk_cfg.judges = (lambda intent: ["request_drift"],)
    r = world.service.submit(make_intent(5_000))
    assert r.state == State.PENDING_APPROVAL and "judge:request_drift" in r.reasons


def test_realtime_auth_declines_mismatched_charge(world):
    r = world.service.submit(make_intent(20_000))
    ok, _ = world.webhooks.decide_authorization(world.auth(r.card_id, 20_000))
    assert ok
    for bad, reason in [
        (world.auth(r.card_id, 20_001), "amount_exceeds_intent"),
        (world.auth(r.card_id, 20_000, name="EVIL STORE"), "merchant_name_mismatch"),
        (world.auth(r.card_id, 20_000, category="betting_casino_gambling"), "mcc_mismatch"),
    ]:
        ok, reasons = world.webhooks.decide_authorization(bad)
        assert not ok and reason in reasons


# ---------------------------------------------------------------- 3. idempotency
def test_lost_response_still_one_card(world):
    """Stripe created the card but the response never arrived. Retry must not make a second."""
    world.stripe.lose_next_response = 1
    r = world.service.submit(make_intent(20_000))
    assert r.state == State.CARD_ISSUED
    assert len(world.stripe.cards) == 1
    assert len({k for name, k in world.stripe.calls if name == "create_card"}) == 1


def test_crash_after_card_creation_then_restart(world):
    def crash(point):
        raise SystemExit(point)

    world.executor.crash_hook = crash
    with pytest.raises(SystemExit):
        world.service.submit(make_intent(20_000))
    iid = world.store.db.execute("SELECT intent_id FROM intents").fetchone()[0]
    assert world.store.get(iid).state == State.APPROVED  # card exists at Stripe, not saved locally

    world.executor.crash_hook = None  # "restart" and resume
    card_id = world.executor.issue_card(iid)
    assert world.executor.issue_card(iid) == card_id  # calling again is a no-op
    assert len(world.stripe.cards) == 1


def test_stripe_down_fails_cleanly_and_releases_budget(world):
    world.stripe.fail_next = 99
    with pytest.raises(Exception):
        world.service.submit(make_intent(20_000))
    iid = world.store.db.execute("SELECT intent_id FROM intents").fetchone()[0]
    assert world.store.get(iid).state == State.FAILED
    assert world.store.budget("it", period_for(world.clock()))["reserved_cents"] == 0


# ---------------------------------------------------------------- 4. webhook-driven state
def test_bad_signature_rejected(world):
    with pytest.raises(InvalidSignature):
        world.webhooks.handle(b'{"id":"evt_x"}', "t=1,v1=deadbeef")


def test_duplicate_webhooks_capture_once(world):
    r = world.service.submit(make_intent(20_000))
    t = world.txn(r.card_id, 20_000)
    world.send("issuing_transaction.created", t, event_id="evt_dup")
    assert world.send("issuing_transaction.created", t, event_id="evt_dup") == {"status": "duplicate"}
    world.send("issuing_transaction.created", t, event_id="evt_other_id_same_txn")
    assert len(world.store.ledger(r.intent_id)) == 1


def test_out_of_order_webhooks_never_move_backward(world):
    r = world.service.submit(make_intent(20_000))
    world.send("issuing_transaction.created", world.txn(r.card_id, 20_000))
    world.send("issuing_authorization.created", world.auth(r.card_id, 20_000))  # arrives late
    assert world.store.get(r.intent_id).state == State.RECONCILED


def test_card_cannot_be_reused_after_capture(world):
    r = world.service.submit(make_intent(20_000))
    world.pay(r.card_id, 20_000)
    ok, reasons = world.webhooks.decide_authorization(world.auth(r.card_id, 1_000))
    assert not ok and "card_not_active_for_state:reconciled" in reasons


# ---------------------------------------------------------------- 5. human approval
def test_approval_flow(world):
    r = world.service.submit(make_intent(60_000))
    assert r.state == State.PENDING_APPROVAL and world.stripe.cards == {}
    done = world.service.approve(r.approval_token, "bob")
    assert done.state == State.CARD_ISSUED


def test_approval_link_is_single_use(world):
    r = world.service.submit(make_intent(60_000))
    world.service.approve(r.approval_token, "bob")
    with pytest.raises(InvalidApproval):
        world.service.approve(r.approval_token, "bob")
    assert len(world.stripe.cards) == 1


def test_wrong_or_self_approval_refused(world):
    r = world.service.submit(make_intent(60_000))
    with pytest.raises(InvalidApproval, match="wrong_approver"):
        world.service.approve(r.approval_token, "alice")
    tampered = r.approval_token.replace(".bob.", ".alice.")
    with pytest.raises(InvalidApproval, match="bad_signature"):
        world.service.approve(tampered, "alice")


def test_rejection_releases_budget(world):
    r = world.service.submit(make_intent(60_000))
    world.service.reject(r.approval_token, "bob")
    assert world.store.budget("it", period_for(world.clock()))["reserved_cents"] == 0


def test_unanswered_approval_expires_and_releases(world):
    r = world.service.submit(make_intent(60_000))
    world.clock.advance(24 * 3600 + 1)
    assert world.service.expire_stale() == [r.intent_id]
    assert world.store.get(r.intent_id).state == State.EXPIRED
    assert world.store.budget("it", period_for(world.clock()))["reserved_cents"] == 0
    with pytest.raises(InvalidApproval):
        world.service.approve(r.approval_token, "bob")


def test_audit_log_explains_every_step(world):
    r = world.service.submit(make_intent(60_000))
    world.service.approve(r.approval_token, "bob")
    steps = [e["to_state"] for e in world.store.audit_log(r.intent_id)]
    assert steps == ["draft", "policy_checked", "pending_approval", "approved", "card_issued"]
