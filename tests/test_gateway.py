"""Known gaps #2 and #4: FakeStripe enforces card controls; LiveStripe maps 5xx to transient."""
import pytest

stripe = pytest.importorskip("stripe")  # `pip install -e ".[dev]"`

from agentpay.models import State
from agentpay.stripe_gateway import LiveStripe, TransientStripeError

from agentpay.testing import World, make_intent


# ---------------------------------------------------------------- card controls alone
def test_card_limit_alone_blocks_inflated_amount(world):
    r = world.service.submit(make_intent(20_000))
    charged, layer = world.checkout(r.card_id, 20_001, realtime=False)
    assert not charged and layer == "card_controls:spending_limit_exceeded"
    assert world.store.ledger(r.intent_id) == []


def test_card_category_alone_blocks_wrong_mcc(world):
    r = world.service.submit(make_intent(20_000))
    charged, layer = world.checkout(r.card_id, 20_000, category="betting_casino_gambling", realtime=False)
    assert not charged and layer == "card_controls:category_not_allowed"


def test_card_controls_cannot_see_merchant_name(world):
    """Documents why the real-time layer exists: Stripe's spending controls can't check the
    merchant name, so a swapped merchant at the right amount and MCC gets through them."""
    r = world.service.submit(make_intent(20_000))
    ok, _ = world.stripe.authorize(r.card_id, 20_000, "usd", "computer_software_stores")
    assert ok
    charged, layer = world.checkout(r.card_id, 20_000, name="EVIL ELECTRONICS")
    assert not charged and layer.startswith("realtime_auth:") and "merchant_name_mismatch" in layer


def test_card_limit_is_all_time_not_per_charge(world):
    r = world.service.submit(make_intent(20_000))
    world.stripe.capture(r.card_id, 15_000)
    ok, why = world.stripe.authorize(r.card_id, 6_000)
    assert not ok and why == "spending_limit_exceeded"
    assert world.stripe.authorize(r.card_id, 5_000) == (True, None)


def test_captured_card_is_canceled_at_stripe_too(world):
    r = world.service.submit(make_intent(20_000))
    assert world.checkout(r.card_id, 20_000) == (True, "charged")
    assert world.store.get(r.intent_id).state == State.RECONCILED
    assert world.stripe.authorize(r.card_id, 100) == (False, "card_canceled")


# ---------------------------------------------------------------- LiveStripe error mapping
class _Boom:
    """Stand-in for stripe.issuing.Card that raises what we tell it to."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        raise self.exc

    def modify(self, *a, **kw):
        self.calls += 1
        raise self.exc


def _live(exc):
    g = LiveStripe("sk_test_fake", "ich_fake")
    g._stripe = type("S", (), {})()
    g._stripe.APIConnectionError = stripe.APIConnectionError
    g._stripe.RateLimitError = stripe.RateLimitError
    g._stripe.APIError = stripe.APIError
    g._stripe.issuing = type("I", (), {})()
    g._stripe.issuing.Card = _Boom(exc)
    return g


def _create(g):
    return g.create_card(intent_id="int_x", amount=100, currency="usd", mcc="computer_software_stores",
                         idempotency_key="k")


@pytest.mark.parametrize("exc", [
    stripe.APIConnectionError("socket closed"),
    stripe.RateLimitError("slow down", http_status=429),
    stripe.APIError("internal", http_status=500),
    stripe.APIError("bad gateway", http_status=502),
    stripe.APIError("unavailable", http_status=503),
    stripe.APIError("no status at all"),  # unknown status: treat as 5xx, retrying is safe with idempotency keys
])
def test_transient_errors_become_transient(exc):
    with pytest.raises(TransientStripeError):
        _create(_live(exc))
    with pytest.raises(TransientStripeError):
        _live(exc).cancel_card("ic_x", idempotency_key="k")


@pytest.mark.parametrize("exc", [
    stripe.InvalidRequestError("no such cardholder", "cardholder", http_status=400),
    stripe.AuthenticationError("bad key", http_status=401),
    stripe.PermissionError("restricted key", http_status=403),
])
def test_client_errors_propagate_unchanged(exc):
    with pytest.raises(type(exc)):
        _create(_live(exc))


def test_live_stripe_refuses_live_keys():
    with pytest.raises(ValueError):
        LiveStripe("sk_live_abc", "ich_x")
