"""HTTP layer: Stripe response contract, session-based approver identity, approval page."""
import json

import pytest

pytest.importorskip("fastapi")  # `pip install -e ".[dev]"`
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from agentpay.app import create_app
from agentpay.auth import COOKIE_NAME, SessionAuth
from agentpay.models import State
from agentpay.webhooks import sign

from agentpay.testing import WEBHOOK_SECRET, World, make_intent

STRIPE_VERSION = "2025-09-30.clover"


@pytest.fixture
def api(world):
    session = SessionAuth(b"session-secret-0123456789", ttl_s=3600)
    app = create_app(world.service, world.webhooks, world.store, session, stripe_version=STRIPE_VERSION,
                     now=world.clock, session_issuer=session, dev_login=True, base_url="http://t")
    client = TestClient(app)
    client.world, client.session = world, session
    return client


def login(api, who):
    c = TestClient(api.app)
    c.cookies.set(COOKIE_NAME, api.session.issue(who, api.world.clock()))
    return c


def post_event(api, etype, obj, event_id="evt_1"):
    payload = json.dumps({"id": event_id, "type": etype, "data": {"object": obj}}).encode()
    return api.post("/webhooks/stripe", content=payload,
                    headers={"Stripe-Signature": sign(payload, WEBHOOK_SECRET, int(api.world.clock()))})


# ---------------------------------------------------------------- Stripe
def test_realtime_auth_response_has_version_header_and_bare_body(api):
    r = api.world.service.submit(make_intent(20_000))
    resp = post_event(api, "issuing_authorization.request", api.world.auth_request(r.card_id, 20_000))
    assert resp.status_code == 200 and resp.headers["Stripe-Version"] == STRIPE_VERSION
    assert resp.json() == {"approved": True}
    resp = post_event(api, "issuing_authorization.request", api.world.auth_request(r.card_id, 99_999))
    assert resp.json() == {"approved": False}


def test_bad_signature_is_400_and_cancel_failure_is_503_for_retry(api):
    assert api.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=00"}).status_code == 400
    r = api.world.service.submit(make_intent(20_000))
    api.world.stripe.cancel_fail_next = 1
    assert post_event(api, "issuing_transaction.created", api.world.txn(r.card_id, 20_000)).status_code == 503
    assert post_event(api, "issuing_transaction.created", api.world.txn(r.card_id, 20_000)).json() == {"status": "ok"}
    assert api.world.store.get(r.intent_id).state == State.RECONCILED
    assert len(api.world.store.ledger(r.intent_id)) == 1


# ---------------------------------------------------------------- auth + approvals
def test_endpoints_require_a_session(api):
    r = api.world.service.submit(make_intent(60_000))
    assert api.post(f"/approvals/{r.approval_token}/approve").status_code == 401
    assert api.post(f"/approvals/{r.approval_token}/approve?approver_id=bob").status_code == 401  # the old hole
    assert api.get(f"/approvals/{r.approval_token}").status_code == 401
    assert api.get(f"/intents/{r.intent_id}").status_code == 401
    assert api.post("/tasks/housekeeping").status_code == 401


def test_approver_identity_comes_from_session_not_request(api):
    r = api.world.service.submit(make_intent(60_000))
    alice = login(api, "alice")  # the requester
    assert alice.post(f"/approvals/{r.approval_token}/approve").status_code == 403  # wrong_approver
    carol = login(api, "carol")
    assert carol.post(f"/approvals/{r.approval_token}/approve").status_code == 403
    bob = login(api, "bob")
    resp = bob.post(f"/approvals/{r.approval_token}/approve")
    assert resp.status_code == 200 and resp.json()["state"] == "card_issued" and resp.json()["decided_by"] == "bob"
    assert bob.post(f"/approvals/{r.approval_token}/approve").status_code == 403  # single use


def test_bearer_header_works_and_expired_session_does_not(api):
    r = api.world.service.submit(make_intent(60_000))
    token = api.session.issue("bob", api.world.clock())
    page = api.get(f"/approvals/{r.approval_token}", headers={"Authorization": f"Bearer {token}"})
    assert page.status_code == 200 and "Purchase approval" in page.text and "$600.00" in page.text
    assert "over_approval_threshold" in page.text and "alice" in page.text
    api.world.clock.advance(3601)
    assert api.get(f"/approvals/{r.approval_token}", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_approval_page_rejects_tampered_link(api):
    r = api.world.service.submit(make_intent(60_000))
    bob = login(api, "bob")
    assert bob.get(f"/approvals/{r.approval_token[:-4]}beef").status_code == 403
    assert bob.post(f"/approvals/{r.approval_token}/maybe").status_code == 404


def test_dev_login_sets_cookie_and_submit_uses_session_identity(api):
    resp = api.post("/auth/dev-login", json={"approver_id": "alice"})
    assert resp.status_code == 200 and COOKIE_NAME in resp.cookies
    body = make_intent(60_000, user="mallory").to_dict()
    body["provenance"] = {"vendor": "catalog", "quantity": "user", "unit_amount": "catalog"}
    resp = api.post("/intents", json=body)  # cookie carried by the client
    assert resp.status_code == 200
    j = resp.json()
    assert j["state"] == "pending_approval" and j["approval_url"].startswith("http://t/approvals/")
    rec = api.world.store.get(j["intent_id"])
    assert rec.intent.user_id == "alice"            # not mallory
    assert rec.intent.provenance == {}              # client-supplied provenance dropped
    assert "untrusted_provenance:vendor" in j["reasons"]  # ...so it is treated as untrusted
    got = api.get(f"/intents/{j['intent_id']}")
    assert got.status_code == 200 and got.json()["state"] == "pending_approval"


def test_dev_login_absent_unless_enabled(world):
    session = SessionAuth(b"session-secret-0123456789")
    app = create_app(world.service, world.webhooks, world.store, session, stripe_version="v")
    assert TestClient(app).post("/auth/dev-login", json={"approver_id": "bob"}).status_code in (404, 405)


def test_housekeeping_endpoint(api):
    r = api.world.service.submit(make_intent(20_000))
    api.world.clock.advance(72 * 3600)
    out = login(api, "ops").post("/tasks/housekeeping").json()
    assert out["swept_cards"] == [r.intent_id]
    assert api.get("/healthz").json() == {"ok": True}
