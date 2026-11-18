"""Regression tests for the bugs found in the 2026-11-18 review (improvement plan, phase 2).

Each section names the bug. All of them got past the eval suite: the evals drive the service
layer single-threaded with a fully configured World, and these bugs lived in the HTTP layer,
the env wiring, multi-threaded use of the store, and webhook shapes the evals never sent.
"""
import json
import threading

import pytest

from agentpay.app import approvers_from_env, user_teams_from_env
from agentpay.approvals import ApprovalSigner, InvalidApproval
from agentpay.models import State
from agentpay.store import period_for
from agentpay.testing import World, make_intent
from agentpay.webhooks import sign

NOW = 1_792_500_000.0


# ---------------------------------------------------------------- 3. dots in approval tokens
@pytest.mark.parametrize("approver", ["bob.smith@corp.com", "a.b.c.d", "bob"])
def test_approval_token_round_trips_ids_containing_dots(approver):
    s = ApprovalSigner(b"k")
    claim = s.verify(s.issue("int_x", approver, NOW), NOW)
    assert (claim.intent_id, claim.approver_id) == ("int_x", approver)
    claim = s.verify(s.issue("int.with.dots", approver, NOW), NOW)
    assert claim.intent_id == "int.with.dots"


def test_approval_token_garbage_fields_are_invalid_not_crashes():
    s = ApprovalSigner(b"k")
    for bad in ("", "a.b.c.d", "a.b.c.d.e.f", "!!.??.n.1.sig"):
        with pytest.raises(InvalidApproval):
            s.verify(bad, NOW)


def test_email_approver_approves_through_the_service():
    w = World()
    w.service.approvers["it"] = "bob.smith@corp.com"
    r = w.service.submit(make_intent(60_000))
    assert w.service.approve(r.approval_token, "bob.smith@corp.com").state == State.CARD_ISSUED


# ---------------------------------------------------------------- 4. team with no approver
def test_team_without_approver_is_rejected_before_reserving(world):
    world.store.set_budget("eng", period_for(world.clock()), 200_000)
    for amount in (60_000, 20_000):  # needs approval / would auto-approve
        r = world.service.submit(make_intent(amount, team="eng"))
        assert r.state == State.REJECTED and r.reasons == ["unknown_team"]
    assert world.remaining_budget("eng") == 200_000
    assert world.store.by_state(State.POLICY_CHECKED) == []


def test_env_refuses_a_budgeted_team_without_an_approver():
    with pytest.raises(ValueError, match="eng"):
        approvers_from_env({}, "it=500000;eng=200000")
    assert approvers_from_env({"APPROVERS": "it=alice;eng=bob.smith@corp.com"}, "it=1;eng=2") == \
        {"it": "alice", "eng": "bob.smith@corp.com"}
    assert approvers_from_env({"IT_APPROVER": "carol"}, "it=1") == {"it": "carol"}  # old variable


def test_env_requires_user_teams_that_point_at_real_teams():
    with pytest.raises(ValueError, match="USER_TEAMS is required"):
        user_teams_from_env({}, {"it": "bob"})
    with pytest.raises(ValueError, match="no approver: eng"):
        user_teams_from_env({"USER_TEAMS": "alice=it;dana=eng"}, {"it": "bob"})
    assert user_teams_from_env({"USER_TEAMS": "*=it"}, {"it": "bob"}) == {"*": "it"}


# ---------------------------------------------------------------- 5. real-time auth fails closed
@pytest.mark.parametrize("mutate", [
    lambda a: a.pop("merchant_data"),
    lambda a: a.update(merchant_data=None),
    lambda a: a.update(merchant_data={"name": "DELL"}),           # no category
    lambda a: a.update(pending_request={"amount": "20000"}),      # wrong type
    lambda a: a.pop("card"),
])
def test_malformed_request_event_is_an_explicit_decline(world, mutate):
    r = world.service.submit(make_intent(20_000))
    auth = world.auth_request(r.card_id, 20_000)
    mutate(auth)
    res = world.send_result("issuing_authorization.request", auth)
    assert res.body == {"approved": False}
    assert not res.decision[0]


def test_decision_error_is_the_reason_when_deciding_raises(world):
    r = world.service.submit(make_intent(20_000))
    auth = world.auth_request(r.card_id, 20_000)
    del auth["merchant_data"]
    assert world.send_result("issuing_authorization.request", auth).decision == (False, ["decision_error"])
    assert "decision_error" in world.store.audit_log(r.intent_id)[-1]["reasons"]


def test_decisions_are_returned_per_request_not_shared(world):
    good = world.service.submit(make_intent(20_000))
    bad = world.service.submit(make_intent(20_000, user="dana"))
    a = world.send_result("issuing_authorization.request", world.auth_request(good.card_id, 20_000))
    b = world.send_result("issuing_authorization.request", world.auth_request(bad.card_id, 99_999))
    assert a.decision == (True, []) and b.decision == (False, ["amount_exceeds_intent"])
    assert not hasattr(world.webhooks, "last_decision")


# ---------------------------------------------------------------- 8. refunds
def test_refund_after_capture_restores_the_budget(world):
    start = world.remaining_budget("it")
    r = world.service.submit(make_intent(20_000))
    world.pay(r.card_id, 20_000)
    assert world.remaining_budget("it") == start - 20_000
    world.send("issuing_transaction.created", world.refund(r.card_id, 20_000))
    assert world.remaining_budget("it") == start
    assert [e["kind"] for e in world.store.ledger(r.intent_id)] == ["capture", "refund"]
    assert sum(e["amount"] for e in world.store.ledger(r.intent_id)) == 0
    assert world.store.get(r.intent_id).state == State.RECONCILED


def test_duplicate_refund_is_recorded_once(world):
    start = world.remaining_budget("it")
    r = world.service.submit(make_intent(20_000))
    world.pay(r.card_id, 20_000)
    refund = world.refund(r.card_id, 5_000, txn_id="ipi_ref")
    world.send("issuing_transaction.created", refund, event_id="evt_a")
    world.send("issuing_transaction.created", refund, event_id="evt_b")  # same txn, new event id
    assert world.remaining_budget("it") == start - 15_000
    assert len([e for e in world.store.ledger(r.intent_id) if e["kind"] == "refund"]) == 1


def test_refund_larger_than_capture_cannot_create_headroom(world):
    start = world.remaining_budget("it")
    r = world.service.submit(make_intent(20_000))
    world.pay(r.card_id, 20_000)
    world.send("issuing_transaction.created", world.refund(r.card_id, 50_000))
    assert world.remaining_budget("it") == start
    assert "refund_exceeds_capture" in world.store.audit_log(r.intent_id)[-1]["reasons"]


def test_unknown_transaction_type_does_not_touch_the_budget(world):
    r = world.service.submit(make_intent(20_000))
    before = world.store.budget("it", period_for(world.clock()))
    world.send("issuing_transaction.created", dict(world.txn(r.card_id, 20_000), type="dispute"))
    assert world.store.budget("it", period_for(world.clock())) == before
    assert world.store.ledger(r.intent_id) == []
    assert world.store.get(r.intent_id).state == State.CARD_ISSUED


def test_over_capture_is_counted_and_flagged(world):
    r = world.service.submit(make_intent(20_000))
    world.send("issuing_transaction.created", world.txn(r.card_id, 21_000))  # force capture
    assert world.store.budget("it", period_for(world.clock()))["spent_cents"] == 21_000
    reasons = {x for e in world.store.audit_log(r.intent_id) for x in e["reasons"]}
    assert "over_capture:21000>20000" in reasons


# ---------------------------------------------------------------- 9. stuck in APPROVED
class Crash(Exception):
    pass


def _crash(point):
    raise Crash(point)


def test_housekeeping_finishes_an_intent_stuck_after_create_card(world):
    world.executor.crash_hook = _crash
    intent = make_intent(20_000)
    with pytest.raises(Crash):
        world.service.submit(intent)
    world.executor.crash_hook = None
    assert world.store.get(intent.intent_id).state == State.APPROVED and len(world.stripe.cards) == 1

    assert world.service.housekeeping()["redriven_approvals"] == []  # too fresh: may still be in flight
    world.clock.advance(6 * 60)
    assert world.service.housekeeping()["redriven_approvals"] == [intent.intent_id]
    rec = world.store.get(intent.intent_id)
    assert rec.state == State.CARD_ISSUED and len(world.stripe.cards) == 1  # Stripe returned the same card
    assert rec.card_id == next(iter(world.stripe.cards))
    assert world.store.by_state(State.APPROVED) == []


def test_stuck_intent_past_the_idempotency_window_fails_and_releases(world):
    start = world.remaining_budget("it")
    world.executor.crash_hook = _crash
    intent = make_intent(20_000)
    with pytest.raises(Crash):
        world.service.submit(intent)
    world.executor.crash_hook = None
    world.clock.advance(25 * 3600)
    world.service.housekeeping()
    assert world.store.get(intent.intent_id).state == State.FAILED
    assert world.remaining_budget("it") == start
    assert len(world.stripe.cards) == 1  # no second card minted


# ---------------------------------------------------------------- 7. store used from many threads
def test_one_store_shared_by_many_threads(tmp_path):
    """The bug: one sqlite3 connection shared by FastAPI's threadpool. 8 threads x 50 submits
    used to produce ~100 errors. A file DB now gets one connection per thread."""
    w = World(path=str(tmp_path / "threads.db"), budget=200_000)
    errors, barrier = [], threading.Barrier(8)

    def worker(t):
        barrier.wait()
        for i in range(50):
            try:
                w.service.submit(make_intent(1_000, user=f"u{t}_{i}"))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    b = w.store.budget("it", period_for(w.clock()))
    assert b["reserved_cents"] + b["spent_cents"] <= b["limit_cents"]
    assert len(w.store.by_state(State.CARD_ISSUED)) == 200 == len(w.stripe.cards)
    assert b["reserved_cents"] == 200 * 1_000


def test_in_memory_store_is_safe_across_threads():
    w = World(budget=50_000)
    errors = []

    def worker(t):
        for i in range(25):
            try:
                w.service.submit(make_intent(1_000, user=f"m{t}_{i}"))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and len(w.store.by_state(State.CARD_ISSUED)) == 50


# ---------------------------------------------------------------- HTTP layer (5, 6, 7)
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from agentpay.app import create_app  # noqa: E402
from agentpay.auth import COOKIE_NAME, SessionAuth  # noqa: E402
from agentpay.testing import WEBHOOK_SECRET  # noqa: E402

HK = "hk-secret"


def make_api(world, user_teams=None):
    session = SessionAuth(b"session-secret-0123456789", ttl_s=3600)
    app = create_app(world.service, world.webhooks, world.store, session, stripe_version="v",
                     user_teams=user_teams or {"alice": "it", "mallory": "it", "dana": "it"},
                     now=world.clock, session_issuer=session, housekeeping_token=HK)

    def as_(who):
        c = TestClient(app)
        c.cookies.set(COOKIE_NAME, session.issue(who, world.clock()))
        return c
    return app, as_


def test_http_malformed_realtime_event_is_200_decline(world):
    app, _ = make_api(world)
    r = world.service.submit(make_intent(20_000))
    auth = world.auth_request(r.card_id, 20_000)
    del auth["merchant_data"]
    payload = json.dumps({"id": "evt_x", "type": "issuing_authorization.request", "data": {"object": auth}}).encode()
    resp = TestClient(app).post("/webhooks/stripe", content=payload,
                                headers={"Stripe-Signature": sign(payload, WEBHOOK_SECRET, int(world.clock()))})
    assert resp.status_code == 200 and resp.json() == {"approved": False}
    assert resp.headers["Stripe-Version"] == "v"


def test_only_requester_and_approver_can_read_an_intent(world):
    _, as_ = make_api(world)
    iid = as_("alice").post("/intents", json=make_intent(60_000).to_dict()).json()["intent_id"]
    assert as_("alice").get(f"/intents/{iid}").status_code == 200
    assert as_("bob").get(f"/intents/{iid}").status_code == 200       # it's approver
    assert as_("mallory").get(f"/intents/{iid}").status_code == 404   # same team, not involved
    assert as_("mallory").get("/intents/int_nope").status_code == 404  # indistinguishable


def test_team_comes_from_identity_not_the_body(world):
    world.store.set_budget("eng", period_for(world.clock()), 500_000)
    world.service.approvers["eng"] = "erin"
    _, as_ = make_api(world)
    body = make_intent(20_000, team="eng").to_dict()
    resp = as_("alice").post("/intents", json=body)
    assert resp.status_code == 403
    assert world.remaining_budget("eng") == 500_000
    body.pop("team")
    resp = as_("alice").post("/intents", json=body)                    # no team given: alice's own
    assert resp.status_code == 200 and world.store.get(resp.json()["intent_id"]).intent.team == "it"
    assert as_("stranger").post("/intents", json=body).status_code == 403  # no team at all


def test_client_cannot_choose_the_intent_id(world):
    _, as_ = make_api(world)
    body = dict(make_intent(20_000).to_dict(), intent_id="int_chosen")
    iid = as_("alice").post("/intents", json=body).json()["intent_id"]
    assert iid != "int_chosen"


def test_approval_page_is_only_for_the_named_approver(world):
    _, as_ = make_api(world)
    r = world.service.submit(make_intent(60_000))
    assert as_("bob").get(f"/approvals/{r.approval_token}").status_code == 200
    assert as_("mallory").get(f"/approvals/{r.approval_token}").status_code == 403


def test_housekeeping_needs_the_scheduler_token(world):
    app, as_ = make_api(world)
    assert as_("alice").post("/tasks/housekeeping").status_code == 403  # a session is not enough
    assert TestClient(app).post("/tasks/housekeeping", headers={"X-Housekeeping-Token": "nope"}).status_code == 403
    assert TestClient(app).post("/tasks/housekeeping", headers={"X-Housekeeping-Token": HK}).status_code == 200
    session = SessionAuth(b"session-secret-0123456789")
    off = create_app(world.service, world.webhooks, world.store, session, stripe_version="v", user_teams={})
    assert TestClient(off).post("/tasks/housekeeping", headers={"X-Housekeeping-Token": ""}).status_code == 403


def test_concurrent_http_submits_on_one_store(tmp_path):
    """Item 7 through the real HTTP stack: sync endpoints on FastAPI's threadpool, one Store."""
    w = World(path=str(tmp_path / "http.db"), budget=100_000)
    _, as_ = make_api(w, user_teams={"*": "it"})
    statuses, barrier = [], threading.Barrier(8)

    def worker(t):
        barrier.wait()
        for i in range(25):
            c = as_(f"h{t}_{i}")
            try:
                statuses.append(c.post("/intents", json=make_intent(1_000).to_dict()).status_code)
            except Exception as e:  # noqa: BLE001
                statuses.append(repr(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert statuses == [200] * 200
    b = w.store.budget("it", period_for(w.clock()))
    assert b["reserved_cents"] + b["spent_cents"] <= b["limit_cents"]
    # Over HTTP, client provenance is dropped, so each order goes to review holding $10.
    assert len(w.store.by_state(State.PENDING_APPROVAL)) == 100  # $1,000 budget / $10 orders
    assert b["reserved_cents"] == 100_000
