"""Known gap #1: a card whose checkout never happened must not stay live forever."""
from agentpay.models import State
from agentpay.store import period_for

from agentpay.testing import World, make_intent


def test_unused_card_is_cancelled_expired_and_budget_released(world):
    r = world.service.submit(make_intent(20_000))
    assert r.state == State.CARD_ISSUED
    world.clock.advance(48 * 3600 + 1)
    assert world.service.sweep_unused_cards(48 * 3600) == [r.intent_id]
    assert world.store.get(r.intent_id).state == State.EXPIRED
    assert world.stripe.cards[r.card_id]["status"] == "canceled"
    assert world.store.budget("it", period_for(world.clock()))["reserved_cents"] == 0
    # A late charge attempt is refused by both layers.
    charged, layer = world.checkout(r.card_id, 20_000)
    assert not charged and layer.startswith("card_controls:card_canceled")
    ok, reasons = world.webhooks.decide_authorization(world.auth_request(r.card_id, 20_000))
    assert not ok and "card_not_active_for_state:expired" in reasons


def test_fresh_card_is_not_swept(world):
    r = world.service.submit(make_intent(20_000))
    world.clock.advance(3600)
    assert world.service.sweep_unused_cards(48 * 3600) == []
    assert world.store.get(r.intent_id).state == State.CARD_ISSUED
    charged, _ = world.checkout(r.card_id, 20_000)
    assert charged and world.store.get(r.intent_id).state == State.RECONCILED


def test_sweep_retries_when_stripe_cancel_fails(world):
    r = world.service.submit(make_intent(20_000))
    world.clock.advance(49 * 3600)
    world.stripe.cancel_fail_next = 1
    assert world.service.sweep_unused_cards() == []            # cancel failed: nothing flipped
    assert world.store.get(r.intent_id).state == State.CARD_ISSUED
    assert world.service.sweep_unused_cards() == [r.intent_id]  # next run finishes the job
    assert world.stripe.cards[r.card_id]["status"] == "canceled"


def test_sweep_ignores_cards_that_were_charged(world):
    r = world.service.submit(make_intent(20_000))
    world.pay(r.card_id, 20_000)
    world.clock.advance(72 * 3600)
    assert world.service.sweep_unused_cards() == []
    assert world.store.get(r.intent_id).state == State.RECONCILED


def test_housekeeping_runs_both_jobs(world):
    pending = world.service.submit(make_intent(60_000))
    issued = world.service.submit(make_intent(20_000, user="dana"))
    world.clock.advance(72 * 3600)
    out = world.service.housekeeping()
    assert out == {"expired_approvals": [pending.intent_id], "swept_cards": [issued.intent_id]}
    assert world.store.budget("it", period_for(world.clock()))["reserved_cents"] == 0
