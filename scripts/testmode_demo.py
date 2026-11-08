"""One real purchase end-to-end against Stripe Issuing TEST MODE.

Prerequisites (all test mode):
  pip install -e ".[server]"
  export STRIPE_SECRET_KEY=sk_test_... STRIPE_CARDHOLDER_ID=ich_... STRIPE_WEBHOOK_SECRET=whsec_... \
         APPROVAL_SECRET=... SESSION_SECRET=... AGENTPAY_DEV_LOGIN=1
  uvicorn agentpay.app:app                                  # terminal 1
  stripe listen --forward-to localhost:8000/webhooks/stripe # terminal 2 (prints the whsec_)
  python scripts/testmode_demo.py [--amount 2000] [--merchant "DELL TECHNOLOGIES"] [--inflate]

What it does
  1. Signs in as the requester and submits an intent through the API (POST /intents).
  2. If the order needs approval, signs in as the approver and approves it (the card is issued).
  3. Uses Stripe's Issuing test helpers to simulate a merchant authorization on that card.
     Stripe sends issuing_authorization.request to the server, which answers in real time.
  4. If approved, captures the authorization; Stripe sends issuing_transaction.created.
  5. Polls GET /intents/{id} until RECONCILED (or prints why it was declined).

`--inflate` asks the merchant for more than the approved amount: expect a decline from the
card's own spending limit and/or the real-time check, and the intent stays CARD_ISSUED
(the sweeper will cancel it later).

Not run in this repo's CI: it needs live credentials. Everything it calls is covered by the
fakes-based tests; this script is the manual test-mode evidence for the build plan.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
import stripe

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from agentpay.config import load_dotenv  # noqa: E402

load_dotenv(os.environ.get("AGENTPAY_ENV_FILE", ".env"))
BASE = os.environ.get("BASE_URL", "http://localhost:8000")


def login(client: httpx.Client, who: str) -> None:
    r = client.post(f"{BASE}/auth/dev-login", json={"approver_id": who})
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=int, default=2_000, help="cents; default $20 (auto-approved)")
    ap.add_argument("--merchant", default="DELL TECHNOLOGIES")
    ap.add_argument("--inflate", action="store_true", help="merchant asks for amount + $5")
    ap.add_argument("--requester", default="alice")
    ap.add_argument("--approver", default=os.environ.get("IT_APPROVER", "approver_it"))
    args = ap.parse_args()

    key = os.environ["STRIPE_SECRET_KEY"]
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit("refusing to run with a non-test key")
    stripe.api_key = key

    with httpx.Client(timeout=30) as c:
        login(c, args.requester)
        intent = {"team": "it", "vendor": "Dell", "mcc": "computer_software_stores", "currency": "usd",
                  "justification": "test-mode demo",
                  "items": [{"sku": "DOCK-1", "description": "USB-C dock", "quantity": 1, "unit_amount": args.amount}]}
        r = c.post(f"{BASE}/intents", json=intent)
        r.raise_for_status()
        sub = r.json()
        print(f"submitted {sub['intent_id']}: {sub['state']} reasons={sub['reasons']}")

        if sub["state"] == "pending_approval":
            login(c, args.approver)
            token = sub["approval_url"].rsplit("/", 1)[-1]
            r = c.post(f"{BASE}/approvals/{token}/approve")
            r.raise_for_status()
            print(f"approved by {args.approver}: {r.json()['state']}")
            login(c, args.requester)
        elif sub["state"] != "card_issued":
            print("order did not reach CARD_ISSUED; nothing to charge")
            return 1

        rec = c.get(f"{BASE}/intents/{sub['intent_id']}").json()
        card_id = rec["card_id"]
        print(f"card issued: {card_id} (limit {args.amount} cents, single use)")

        ask = args.amount + (500 if args.inflate else 0)
        print(f"merchant {args.merchant!r} requests {ask} cents ...")
        try:
            auth = stripe.issuing.Authorization.TestHelpers.create(
                amount=ask, currency="usd", card=card_id,
                merchant_data={"category": "computer_software_stores", "name": args.merchant,
                               "city": "Round Rock", "country": "US"},
            )
        except stripe.InvalidRequestError as e:
            # Card controls (spending limit / category) decline before any webhook is sent.
            print(f"declined by Stripe card controls before our webhook ran: {e.user_message or e}")
            return 0
        print(f"authorization {auth.id}: approved={auth.approved} status={auth.status} "
              f"reason={auth.request_history[-1].reason if auth.request_history else '?'}")
        if not auth.approved:
            print("declined by our real-time check (see server log for reasons); card stays CARD_ISSUED "
                  "until the sweeper cancels it")
            return 0

        txn = stripe.issuing.Authorization.TestHelpers.capture(auth.id, capture_amount=ask)
        print(f"captured: {txn.id if hasattr(txn, 'id') else txn}")

        for _ in range(30):
            state = c.get(f"{BASE}/intents/{sub['intent_id']}").json()["state"]
            print(f"  state: {state}")
            if state == "reconciled":
                card = stripe.issuing.Card.retrieve(card_id)
                print(f"done. card status at Stripe: {card.status}")
                return 0
            time.sleep(1)
        print("timed out waiting for webhooks — is `stripe listen` forwarding to the server?")
        return 1


if __name__ == "__main__":
    sys.exit(main())
