"""Signed, expiring, single-use approval links bound to a named approver.

Token: <b64(intent_id)>.<b64(approver_id)>.<nonce>.<expires_at>.<hmac>
The id fields are base64url-encoded (no padding) so a `.` inside them, as in every email
address, can't shift the field boundaries. base64url's alphabet has no `.` and is URL-safe.
Single-use is enforced by Store.use_nonce, not by the token itself.
"""
from __future__ import annotations

import base64
import binascii
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


def _enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()


def _dec(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


class ApprovalSigner:
    def __init__(self, secret: bytes, ttl_s: int = 24 * 3600):
        self.secret = secret
        self.ttl_s = ttl_s

    def issue(self, intent_id: str, approver_id: str, now: float) -> str:
        nonce = secrets.token_hex(8)
        body = f"{_enc(intent_id)}.{_enc(approver_id)}.{nonce}.{int(now) + self.ttl_s}"
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, now: float) -> ApprovalClaim:
        try:
            iid_b64, approver_b64, nonce, exp, sig = token.split(".")
        except ValueError:
            raise InvalidApproval("malformed")
        body = f"{iid_b64}.{approver_b64}.{nonce}.{exp}"
        if not hmac.compare_digest(sig, self._sign(body)):
            raise InvalidApproval("bad_signature")
        try:
            intent_id, approver_id, expires_at = _dec(iid_b64), _dec(approver_b64), int(exp)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise InvalidApproval("malformed")
        if now > expires_at:
            raise InvalidApproval("expired")
        return ApprovalClaim(intent_id, approver_id, nonce, expires_at)

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
