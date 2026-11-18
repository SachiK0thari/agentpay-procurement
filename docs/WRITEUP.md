# agentpay: letting an AI agent buy things without trusting it with money

**Summary.** agentpay is a procurement agent built on Stripe Issuing. A teammate asks for
"3 USB-C docks for the new hires". Claude plans the order. Ordinary, tested code decides whether
any money moves. Across 88 adversarial and failure scenarios it made **0 unsafe purchases** and
**0 duplicate charges**. A real purchase also ran end to end in a Stripe Issuing test-mode
sandbox (2026-11-15).

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
| Scenarios | 88 across 7 groups (happy, policy edge, adversarial, card controls only, lifecycle, failure, planner) |
| Unsafe purchases | **0** |
| Adversarial scenarios blocked | 18 / 18 |
| Duplicate charges under crashes, retries and duplicate webhooks | 0 |
| Legitimate orders completed | 90.9%. Every miss is a deliberate fail-closed outcome: Stripe unavailable, or the model refused or never proposed. |
| Sent to human review | 34.1% |

The `card_controls_only` group switches off the real-time check to show that the card's own
limit and category control stop inflated and off-category charges by themselves. 167 unit tests
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

## Bugs found in review

A code review after the first version found bugs that 82 scenarios and 128 tests had missed,
all with 0 unsafe purchases on the board. They were missed for one reason: the evals drove
the service layer directly, on one thread, with captures only. The HTTP layer, the threadpool
FastAPI runs sync endpoints on, and refunds were never exercised. Each fix has a regression test
in `tests/test_review_regressions.py`, and most have a new eval scenario.

| Bug | Why the evals missed it | Now covered by |
| --- | --- | --- |
| Approval tokens were joined with `.`, so an email approver id (`bob.smith@corp.com`) could never approve. | Every test approver was a bare name like `alice`. | Token round-trip with dotted ids; an email approver approves end to end. |
| A team with a budget but no approver reserved funds, then crashed, leaving money held forever. | Every eval team had an approver. | Unknown teams rejected before reserving; startup refuses a budgeted team with no approver; `edge-team-without-approver`. |
| A malformed real-time authorization event raised a 500, which hands the decision to Stripe's timeout fallback. | Evals only sent well-formed events. | Any decision error is an explicit decline; HTTP test for a missing `merchant_data`. |
| Any signed-in user could read any intent, spend any team's budget, or run housekeeping. | Evals called the service, not the HTTP API, so there was no "who is calling" to get wrong. | Requester/approver-only reads, team taken from identity, a scheduler token for housekeeping. |
| One SQLite connection was shared across FastAPI's threadpool: 99 errors in 400 concurrent submits. | The concurrency test opened a separate store per worker. | A store shared by many threads, and concurrent HTTP submits against one store. |
| Refunds were counted as spend, so a refund doubled the spent total. | The fake only emitted captures. | Refunds restore the budget, are recorded once and can't create headroom; `lifecycle-purchase-then-full-refund`, `-partial-refund`. |
| A crash between creating the card and saving it left the intent approved with its budget reserved. | The crash scenarios only covered points the code already recovered from. | Housekeeping re-drives stuck intents with the same idempotency key; `fail-crash-after-card-housekeeping-recovers`. |
| "3 docks and 5 monitors" proposed as 5 docks and 3 monitors passed as user-stated. | Quantities were checked against the set of stated numbers, not bound to items. | Each quantity is bound to its item; `planner-injected-quantity-swap`. |

The lesson: an eval suite that only reaches the core can report 0 unsafe while the edges are
wide open. The suite now also covers the HTTP layer, concurrency and the full payment lifecycle.

## Limitations

- **Real Stripe is test mode only, and the evals use an in-memory Stripe.** One real test-mode
  purchase ran end to end in a Stripe sandbox (2026-11-15): card issued, Stripe asked our server
  in real time, capture, reconcile, card canceled. Against the same sandbox, an over-limit
  charge was declined by Stripe's card controls and a look-alike merchant by our real-time check.
  The 88 scenarios themselves run against FakeStripe so they're fast and repeatable.
- **Live planner coverage is thin.** Against real Claude (`--live-planner`, 2026-11-14) all 21
  proposals matched the request exactly, but the model opened the injected product page in only
  2 of 21 runs, so most injections never reached it. The scripted evals are what show the
  guardrails hold when the model does fall for a page.
- **Provenance matching is rule-based.** Numbers after IDs ("SKU 50", "ticket 7"), prices, sizes
  and times no longer count as quantities, and a request form's quantity field overrides the
  text. Phrasings the rules don't anticipate still fall to human review rather than through.
- **Not production infrastructure.** It uses SQLite and has no scheduler for
  the expiry and card-sweep job. DESIGN.md §9 lists what production would add.

## Try it

```bash
pip install -e ".[server,dev]"
pytest -q                                  # 167 tests, no network
python evals/run_evals.py                  # 88 scenarios, metrics table
python scripts/local_demo.py --pause       # the four-scene walkthrough from the video
```

The full design, decisions and threat model are in DESIGN.md.
