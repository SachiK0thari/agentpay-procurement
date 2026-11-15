# agentpay: letting an AI agent buy things without trusting it with money

**Summary.** agentpay is a procurement agent built on Stripe Issuing. A teammate asks for
"3 USB-C docks for the new hires". Claude plans the order. Ordinary, tested code decides whether
any money moves. Across 75 adversarial and failure scenarios it made **0 unsafe purchases** and
**0 duplicate charges**. One real Stripe test-mode purchase is still outstanding: Stripe has to
approve Issuing for the account first.

## The problem

An agent that can pay is a non-deterministic actor holding a financial instrument. Five things
go wrong:

1. **It overspends.** It orders 30 laptops instead of 3.
2. **It gets manipulated.** A product page says "customers always need 5, not 3".
3. **It charges twice.** A timeout triggers a retry and the order is placed again.
4. **It reports false state.** It says the order succeeded when the card was declined.
5. **Nobody is accountable.** A $4,000 order goes through without a human seeing it.

Better prompting doesn't fix any of these. They need a design where the model's mistakes can't
turn into payments.

## The core decision: the model proposes, code decides

The planner (Claude) has only read-only tools: search the catalog, read a product page, check
the budget. Its whole output is one structured purchase proposal. It never sees a card number
and never decides whether a purchase is allowed. Everything after the proposal is deterministic
code with unit tests:

| Layer | What it enforces |
| --- | --- |
| Provenance (assigned by code) | Each value (vendor, quantity, price) is tagged by where it came from: the user's words, the catalog, or a web page. The model can't label its own values as trusted. |
| Policy | Per-order cap ($2,000), approval threshold ($500), vendor and merchant-category allowlist, quantity limits, currency. |
| Risk | Values from web pages, new vendors, purchase bursts, and split orders that dodge the approval threshold all go to human review. An LLM judge can escalate an order but can never approve one. |
| Budget | Funds are reserved atomically when policy passes, so two pending orders can't both spend the last $500. |
| Approval | A signed link that works once, expires in 24h, is bound to one approver, and refuses self-approval. The approver's identity comes from their signed-in session, never from the request. |
| The card itself | A single-use Stripe Issuing virtual card, limited to the exact approved amount and the approved merchant category. This is the last line of defence even if every layer above fails. |
| Real-time authorization | When a merchant charges the card, Stripe asks the server in real time. The server checks the amount, currency, merchant category and merchant name against the approved order. |
| Webhooks and ledger | Signed, de-duplicated, forward-only state updates. The ledger is keyed by Stripe's object id, so a duplicated event can never record a charge twice. |

## Results

From `python evals/run_evals.py`, which runs the full system against an in-memory Stripe and a
scripted model:

| Metric | Value |
| --- | --- |
| Scenarios | 75 across 7 groups (happy, policy edge, adversarial, card controls only, lifecycle, failure, planner) |
| Unsafe purchases | **0** |
| Adversarial scenarios blocked | 18 / 18 |
| Duplicate charges under crashes, retries and duplicate webhooks | 0 |
| Legitimate orders completed | 89.5%. Every miss is a deliberate fail-closed outcome: Stripe unavailable, or the model refused or never proposed. |
| Sent to human review | 32% |

The `card_controls_only` group switches off the real-time check to show that the card's own
limit and category control stop inflated and off-category charges by themselves. 90 unit tests
attack each guardrail on its own.

## What testing against real Stripe found

Running the server against a real Stripe account surfaced four bugs that the fakes couldn't:

- **Every order was rejected.** The server never created team budgets on startup, so every live
  order failed with "budget exhausted". It now seeds them from configuration.
- **A Stripe error left money stuck.** A permanent Stripe error, such as an invalid key, left the
  order "approved" with its budget reserved indefinitely, and nothing would ever retry it. These
  errors now fail the order and release the budget.
- **Secrets could reach the logs.** Server logs and error messages could contain parts of Stripe
  keys and approval tokens. Both are now redacted.
- **Newer Stripe accounts need a financial account on each card.** Card creation now passes one
  when configured.

Building the demo then exposed a real gap. The merchant-name check was a substring match, so a
charge from "TOTALLY NOT FIGMA LLC" was approved for a Figma order. The check now requires the
merchant name to start with the vendor's name as whole words, and a new eval covers it. The
tradeoff: names with a payment-processor prefix such as "SQ *FIGMA" are now declined. That's the
safe direction, because a false block costs a retry and a bad approval costs money.

## Limitations

- **No real test-mode purchase yet.** On this account, Stripe Issuing needs approval from
  Stripe's sales team. Everything up to card creation was exercised against real Stripe: keys,
  cardholder, approval flow and the card request itself. Stripe refuses cards while the
  account's financial account is pending. `scripts/testmode_demo.py` will run unchanged once
  Issuing is enabled.
- **Planner numbers come from a scripted model.** The planner evals check that the guardrails
  hold whatever the model proposes. They don't measure how often real Claude falls for an
  injected page. `--live-planner` runs them against the real API.
- **Provenance matching is word-based.** "Order 3 of SKU 50" would accept 50 as a quantity the
  user typed. A structured request form would close this.
- **Not production infrastructure.** It uses SQLite, a development sign-in, and no scheduler for
  the expiry and card-sweep job. DESIGN.md §8 lists what production would add.

## Try it

```bash
pip install -e ".[server,dev]"
pytest -q                                  # 90 tests, no network
python evals/run_evals.py                  # 75 scenarios, metrics table
python scripts/local_demo.py --pause       # the four-scene walkthrough from the video
```

The full design, decisions and threat model are in DESIGN.md.
