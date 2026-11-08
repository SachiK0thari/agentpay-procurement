"""SQLite persistence: intents, budgets, audit log, ledger, webhook dedupe, approval nonces.

Concurrency-sensitive writes use compare-and-set UPDATEs (`WHERE state = ?`,
`WHERE remaining >= ?`) so two workers can never both win. Swap for Postgres in
production; the SQL is deliberately portable.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .models import PurchaseIntent, State
from .state_machine import PAYMENT_RANK, TERMINAL, check

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id     TEXT PRIMARY KEY,
    data          TEXT NOT NULL,
    state         TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    team          TEXT NOT NULL,
    vendor        TEXT NOT NULL,
    amount        INTEGER NOT NULL,
    budget_period TEXT NOT NULL,
    reserved      INTEGER NOT NULL DEFAULT 0,
    card_id       TEXT UNIQUE,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_user ON intents(user_id, created_at);
CREATE TABLE IF NOT EXISTS budgets (
    team          TEXT NOT NULL,
    period        TEXT NOT NULL,
    limit_cents   INTEGER NOT NULL,
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    spent_cents   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (team, period)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]', at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    ref TEXT PRIMARY KEY,          -- Stripe object id, so a capture is recorded once
    intent_id TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval_nonces (nonce TEXT PRIMARY KEY, intent_id TEXT, at REAL NOT NULL);
"""


@dataclass
class IntentRecord:
    intent: PurchaseIntent
    state: State
    reserved: int
    card_id: Optional[str]
    created_at: float
    budget_period: str


def period_for(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


class Store:
    def __init__(self, path: str = ":memory:", now: Callable[[], float] = time.time):
        self.now = now
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---------- intents & state ----------
    def create(self, intent: PurchaseIntent) -> None:
        ts = self.now()
        self.db.execute(
            "INSERT INTO intents (intent_id, data, state, user_id, team, vendor, amount,"
            " budget_period, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (intent.intent_id, json.dumps(intent.to_dict()), State.DRAFT.value, intent.user_id,
             intent.team, intent.vendor, intent.amount, period_for(ts), ts, ts),
        )
        self._audit(intent.intent_id, None, State.DRAFT, [])

    def get(self, intent_id: str) -> IntentRecord:
        row = self.db.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return self._record(row)

    def get_by_card(self, card_id: str) -> Optional[IntentRecord]:
        row = self.db.execute("SELECT * FROM intents WHERE card_id = ?", (card_id,)).fetchone()
        return self._record(row) if row else None

    def transition(self, intent_id: str, to: State, reasons: list[str] | None = None) -> bool:
        """Compare-and-set move. Returns False if another worker moved it first."""
        current = self.get(intent_id).state
        check(current, to)
        cur = self.db.execute(
            "UPDATE intents SET state = ?, updated_at = ? WHERE intent_id = ? AND state = ?",
            (to.value, self.now(), intent_id, current.value),
        )
        if cur.rowcount == 1:
            self._audit(intent_id, current, to, reasons or [])
            return True
        return False

    def advance(self, intent_id: str, to: State) -> bool:
        """Forward-only move for webhook-driven states. Late or duplicate events are no-ops."""
        current = self.get(intent_id).state
        if current in TERMINAL or current not in PAYMENT_RANK:
            return False
        if PAYMENT_RANK[current] >= PAYMENT_RANK[to]:
            return False
        return self.transition(intent_id, to)

    def attach_card(self, intent_id: str, card_id: str) -> bool:
        cur = self.db.execute(
            "UPDATE intents SET card_id = ?, state = ?, updated_at = ?"
            " WHERE intent_id = ? AND state = ? AND card_id IS NULL",
            (card_id, State.CARD_ISSUED.value, self.now(), intent_id, State.APPROVED.value),
        )
        if cur.rowcount == 1:
            self._audit(intent_id, State.APPROVED, State.CARD_ISSUED, [f"card:{card_id}"])
            return True
        return False

    def recent_for_user(self, user_id: str, since: float) -> list[IntentRecord]:
        rows = self.db.execute(
            "SELECT * FROM intents WHERE user_id = ? AND created_at >= ? AND state != ?",
            (user_id, since, State.REJECTED.value),
        ).fetchall()
        return [self._record(r) for r in rows]

    def vendor_known(self, team: str, vendor: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM intents WHERE team = ? AND vendor = ? AND state = ? LIMIT 1",
            (team, vendor, State.RECONCILED.value),
        ).fetchone()
        return row is not None

    def stale_pending(self, older_than: float) -> list[str]:
        return self._stale(State.PENDING_APPROVAL, older_than)

    def stale_issued(self, older_than: float) -> list[str]:
        """Cards issued but never charged — e.g. the merchant declined or the user gave up."""
        return self._stale(State.CARD_ISSUED, older_than)

    def by_state(self, state: State) -> list[IntentRecord]:
        rows = self.db.execute("SELECT * FROM intents WHERE state = ? ORDER BY created_at", (state.value,)).fetchall()
        return [self._record(r) for r in rows]

    def note(self, intent_id: str, reasons: list[str]) -> None:
        """Audit-only entry (no state change), e.g. a real-time authorization decision."""
        current = self.get(intent_id).state
        self._audit(intent_id, current, current, reasons)

    def audit_log(self, intent_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT from_state, to_state, reasons, at FROM audit WHERE intent_id = ? ORDER BY id",
            (intent_id,),
        ).fetchall()
        return [dict(r) | {"reasons": json.loads(r["reasons"])} for r in rows]

    # ---------- budgets ----------
    def set_budget(self, team: str, period: str, limit_cents: int) -> None:
        self.db.execute(
            "INSERT INTO budgets (team, period, limit_cents) VALUES (?,?,?)"
            " ON CONFLICT(team, period) DO UPDATE SET limit_cents = excluded.limit_cents",
            (team, period, limit_cents),
        )

    def budget(self, team: str, period: str) -> dict:
        row = self.db.execute("SELECT * FROM budgets WHERE team = ? AND period = ?", (team, period)).fetchone()
        return dict(row) if row else {}

    def reserve(self, intent_id: str) -> bool:
        """Atomically hold budget for an intent. Fails if the team can't afford it."""
        rec = self.get(intent_id)
        amount = rec.intent.amount
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE budgets SET reserved_cents = reserved_cents + ?"
                " WHERE team = ? AND period = ? AND limit_cents - spent_cents - reserved_cents >= ?",
                (amount, rec.intent.team, rec.budget_period, amount),
            )
            if cur.rowcount != 1:
                self.db.execute("ROLLBACK")
                return False
            self.db.execute("UPDATE intents SET reserved = ? WHERE intent_id = ?", (amount, intent_id))
            self.db.execute("COMMIT")
            return True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def release(self, intent_id: str) -> None:
        self._settle(intent_id, spent=0)

    def record_capture(self, intent_id: str, amount: int, ref: str) -> bool:
        """Write the ledger entry and convert the reservation into spend, exactly once."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO ledger (ref, intent_id, kind, amount, at) VALUES (?,?,?,?,?)",
                (ref, intent_id, "capture", amount, self.now()),
            )
            if cur.rowcount != 1:
                self.db.execute("ROLLBACK")
                return False
            self._settle(intent_id, spent=amount, in_txn=True)
            self.db.execute("COMMIT")
            return True
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def ledger(self, intent_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM ledger", ()
        if intent_id:
            q, args = q + " WHERE intent_id = ?", (intent_id,)
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    # ---------- dedupe ----------
    def event_seen(self, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)).fetchone() is not None

    def mark_event(self, event_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO processed_events (event_id, at) VALUES (?,?)", (event_id, self.now()))

    def use_nonce(self, nonce: str, intent_id: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO approval_nonces (nonce, intent_id, at) VALUES (?,?,?)",
            (nonce, intent_id, self.now()),
        )
        return cur.rowcount == 1

    # ---------- internals ----------
    def _stale(self, state: State, older_than: float) -> list[str]:
        rows = self.db.execute(
            "SELECT intent_id FROM intents WHERE state = ? AND updated_at < ?",
            (state.value, older_than),
        ).fetchall()
        return [r["intent_id"] for r in rows]

    def _settle(self, intent_id: str, spent: int, in_txn: bool = False) -> None:
        if not in_txn:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            rec = self.get(intent_id)
            self.db.execute(
                "UPDATE budgets SET reserved_cents = reserved_cents - ?, spent_cents = spent_cents + ?"
                " WHERE team = ? AND period = ?",
                (rec.reserved, spent, rec.intent.team, rec.budget_period),
            )
            self.db.execute("UPDATE intents SET reserved = 0 WHERE intent_id = ?", (intent_id,))
            if not in_txn:
                self.db.execute("COMMIT")
        except Exception:
            if not in_txn:
                self.db.execute("ROLLBACK")
            raise

    def _audit(self, intent_id: str, src: State | None, dst: State, reasons: list[str]) -> None:
        self.db.execute(
            "INSERT INTO audit (intent_id, from_state, to_state, reasons, at) VALUES (?,?,?,?,?)",
            (intent_id, src.value if src else None, dst.value, json.dumps(reasons), self.now()),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> IntentRecord:
        return IntentRecord(
            intent=PurchaseIntent.from_dict(json.loads(row["data"])),
            state=State(row["state"]),
            reserved=row["reserved"],
            card_id=row["card_id"],
            created_at=row["created_at"],
            budget_period=row["budget_period"],
        )
