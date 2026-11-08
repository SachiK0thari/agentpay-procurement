"""Signed, expiring, single-use approval links bound to a named approver.

Token: <intent_id>.<approver_id>.<nonce>.<expires_at>.<hmac>
Single-use is enforced by Store.use_nonce, not by the token itself.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


class InvalidApproval(Exception):
    pass


@dataclass(frozen=True)
class ApprovalClaim:
    intent_id: str
    approver_id: str
    nonce: str
    expires_at: int


class ApprovalSigner:
    def __init__(self, secret: bytes, ttl_s: int = 24 * 3600):
        self.secret = secret
        self.ttl_s = ttl_s

    def issue(self, intent_id: str, approver_id: str, now: float) -> str:
        nonce = secrets.token_hex(8)
        body = f"{intent_id}.{approver_id}.{nonce}.{int(now) + self.ttl_s}"
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, now: float) -> ApprovalClaim:
        try:
            intent_id, approver_id, nonce, exp, sig = token.split(".")
        except ValueError:
            raise InvalidApproval("malformed")
        body = f"{intent_id}.{approver_id}.{nonce}.{exp}"
        if not hmac.compare_digest(sig, self._sign(body)):
            raise InvalidApproval("bad_signature")
        if now > int(exp):
            raise InvalidApproval("expired")
        return ApprovalClaim(intent_id, approver_id, nonce, int(exp))

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
