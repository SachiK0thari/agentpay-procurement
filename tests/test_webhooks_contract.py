"""Known gap #6: the real-time authorization contract as Stripe actually sends it."""
import json

import pytest

from agentpay.webhooks import InvalidSignature, requested_amount, sign

from agentpay.testing import WEBHOOK_SECRET, World, make_intent


def test_request_event_amount_is_read_from_pending_request(world):
    """On issuing_authorization.request the top-level amount is 0. Reading it there would
    approve every inflated charge."""
    r = world.service.submit(make_intent(20_000))
    inflated = world.auth_request(r.card_id, 25_000)
    assert inflated["amount"] == 0
    ok, reasons = world.webhooks.decide_authorization(inflated)
    assert not ok and reasons == ["amount_exceeds_intent"]
    assert world.webhooks.decide_authorization(world.auth_request(r.card_id, 20_000)) == (True, [])


def test_requested_amount_falls_back_to_top_level_on_created_events(world):
    assert requested_amount(world.auth("ic_1", 1234)) == (1234, "usd")
    assert requested_amount(world.auth_request("ic_1", 999, currency="eur")) == (999, "eur")


def test_request_event_currency_from_pending_request(world):
    r = world.service.submit(make_intent(20_000))
    ok, reasons = world.webhooks.decide_authorization(world.auth_request(r.card_id, 20_000, currency="eur"))
    assert not ok and "currency_mismatch" in reasons


def test_zero_amount_request_is_declined(world):
    r = world.service.submit(make_intent(20_000))
    ok, reasons = world.webhooks.decide_authorization(world.auth_request(r.card_id, 0))
    assert not ok and "non_positive_amount" in reasons


def test_handle_returns_exact_stripe_body_and_audits_reasons(world):
    r = world.service.submit(make_intent(20_000))
    body = world.send("issuing_authorization.request", world.auth_request(r.card_id, 20_001))
    assert body == {"approved": False}  # nothing else: Stripe validates the body against the API version
    assert world.webhooks.last_decision == (False, ["amount_exceeds_intent"])
    last = world.store.audit_log(r.intent_id)[-1]
    assert last["from_state"] == last["to_state"] == "card_issued"
    assert "amount_exceeds_intent" in last["reasons"]


def test_request_events_are_not_deduped(world):
    """Stripe sends a fresh request per authorization attempt; each gets its own decision."""
    r = world.service.submit(make_intent(20_000))
    for _ in range(2):
        assert world.send("issuing_authorization.request", world.auth_request(r.card_id, 20_000),
                          event_id="evt_same") == {"approved": True}


def test_unknown_card_is_declined(world):
    assert world.webhooks.decide_authorization(world.auth_request("ic_nobody", 100)) == (False, ["unknown_card"])


def test_stale_timestamp_and_garbage_header_rejected(world):
    payload = json.dumps({"id": "evt_1", "type": "x", "data": {"object": {}}}).encode()
    with pytest.raises(InvalidSignature, match="tolerance"):
        world.webhooks.handle(payload, sign(payload, WEBHOOK_SECRET, int(world.clock()) - 3600))
    with pytest.raises(InvalidSignature):
        world.webhooks.handle(payload, "t=abc,v1=00")
    with pytest.raises(InvalidSignature):
        world.webhooks.handle(payload, "nonsense")


def test_cancel_failure_after_capture_is_retried_without_double_count(world):
    r = world.service.submit(make_intent(20_000))
    world.stripe.cancel_fail_next = 1
    charged, _ = world.checkout(r.card_id, 20_000)  # first delivery raises, retry succeeds
    assert charged
    assert len(world.store.ledger(r.intent_id)) == 1
    assert world.store.get(r.intent_id).state.value == "reconciled"
    assert world.stripe.cards[r.card_id]["status"] == "canceled"
