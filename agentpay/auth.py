"""Who is calling the HTTP API. Approvals are bound to an approver id, so identity must come
from an authenticated session — never from a query parameter the caller types in.

`SessionAuth` issues and verifies compact HMAC-signed session tokens (JWT-shaped: base64url
JSON payload + signature) carried as a cookie or `Authorization: Bearer`. It is the dev /
test-mode implementation. In production, plug in your identity provider: anything with
`verify(token, now) -> subject` works (e.g. an OIDC/JWT verifier for your SSO), and the
approval endpoints don't change.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Protocol

COOKIE_NAME = "agentpay_session"


class Unauthenticated(Exception):
    pass


class Authenticator(Protocol):
    def verify(self, token: str, now: float) -> str:
        """Return the authenticated subject (user id) or raise Unauthenticated."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SessionAuth:
    def __init__(self, secret: bytes, ttl_s: int = 8 * 3600):
        if len(secret) < 16:
            raise ValueError("session secret too short")
        self.secret = secret
        self.ttl_s = ttl_s

    def issue(self, subject: str, now: float) -> str:
        payload = _b64(json.dumps({"sub": subject, "exp": int(now) + self.ttl_s,
                                   "nonce": secrets.token_hex(8)}, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str, now: float) -> str:
        try:
            payload, sig = token.split(".")
        except ValueError:
            raise Unauthenticated("malformed")
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise Unauthenticated("bad_signature")
        try:
            claims = json.loads(_unb64(payload))
            sub, exp = str(claims["sub"]), int(claims["exp"])
        except (ValueError, KeyError, TypeError):
            raise Unauthenticated("malformed")
        if now > exp:
            raise Unauthenticated("expired")
        return sub

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
