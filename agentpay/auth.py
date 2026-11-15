"""Who is calling the HTTP API. Approvals are bound to an approver id, so identity must come
from an authenticated session — never from a query parameter the caller types in.

Two implementations of `verify(token, now) -> subject`; the endpoints don't care which:

`SessionAuth`  compact HMAC-signed session tokens (JWT-shaped: base64url JSON payload +
               signature) carried as a cookie or `Authorization: Bearer`. Dev / test mode,
               paired with `POST /auth/dev-login`.
`OIDCAuth`     tokens signed by your identity provider (Okta, Entra ID, Google Workspace,
               Auth0, Keycloak...). Verifies the signature against the IdP's published keys
               (JWKS), plus issuer, audience, expiry and the subject claim. Needs the `sso`
               extra (`pip install -e .[sso]`). The app doesn't run a login flow itself: put it
               behind your SSO proxy / gateway, which forwards the ID token as
               `Authorization: Bearer`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.request
from typing import Any, Callable, Iterable, Optional, Protocol

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


# ---------------------------------------------------------------- OIDC / SSO
# Asymmetric only. "none" and HS* are never accepted: with HS256 a token "signed" with the
# public key would verify (algorithm confusion).
DEFAULT_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA")
JwksFetch = Callable[[str], dict]


def _fetch_json(url: str) -> dict:
    if not url.startswith("https://"):
        raise Unauthenticated("idp_url_not_https")
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (https enforced above)
        return json.loads(r.read())


class OIDCAuth:
    """Verify tokens issued by an OIDC identity provider.

    issuer          exact `iss` value, e.g. "https://acme.okta.com"
    audience        the client id this app is registered as (`aud`)
    jwks / jwks_uri static key set (tests, air-gapped) or its URL; if both are omitted the URL is
                    discovered from `{issuer}/.well-known/openid-configuration`
    subject_claim   which claim names the user. "sub" is stable but opaque; "email" matches
                    human-readable approver ids (IT_APPROVER=it-lead@acme.com) and then
                    requires `email_verified: true`
    allowed_domains if set, the subject's email domain must be one of these
    """

    def __init__(self, issuer: str, audience: str, *, jwks: Optional[dict] = None,
                 jwks_uri: Optional[str] = None, subject_claim: str = "sub",
                 algorithms: Iterable[str] = DEFAULT_ALGORITHMS, leeway_s: int = 60,
                 allowed_domains: Optional[Iterable[str]] = None, fetch: JwksFetch = _fetch_json,
                 refetch_interval_s: int = 300):
        import jwt  # sso extra; imported here so SessionAuth needs no dependency

        self._jwt = jwt
        algorithms = tuple(algorithms)
        if not algorithms or any(a.lower() == "none" or a.upper().startswith("HS") for a in algorithms):
            raise ValueError("OIDCAuth accepts asymmetric algorithms only")
        if not issuer or not audience:
            raise ValueError("issuer and audience are required")
        self.issuer = issuer
        self.audience = audience
        self.subject_claim = subject_claim
        self.algorithms = algorithms
        self.leeway_s = leeway_s
        self.allowed_domains = {d.lower().lstrip("@") for d in allowed_domains} if allowed_domains else None
        self._fetch = fetch
        self._jwks_uri = jwks_uri
        self._static = jwks is not None
        self._keys: dict[str, Any] = self._parse(jwks) if jwks is not None else {}
        self._last_fetch = float("-inf")
        self._refetch_interval_s = refetch_interval_s
        self._lock = threading.Lock()

    # ---------- keys ----------
    def _parse(self, jwks: dict) -> dict[str, Any]:
        keys = {}
        for k in jwks.get("keys", []):
            if k.get("use", "sig") != "sig":
                continue
            try:
                keys[k.get("kid", "")] = (self._jwt.PyJWK(k), k.get("alg"))
            except self._jwt.PyJWTError:
                continue  # unsupported key type: skip it rather than fail the whole set
        return keys

    def _refresh(self) -> None:
        """Fetch the key set, at most once per refetch interval (unknown kids can't force a
        fetch storm against the IdP)."""
        if self._static:
            return
        with self._lock:
            mono = time.monotonic()
            if mono - self._last_fetch < self._refetch_interval_s:
                return
            self._last_fetch = mono
            try:
                if self._jwks_uri is None:
                    conf = self._fetch(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
                    if conf.get("issuer") != self.issuer:
                        raise Unauthenticated("idp_issuer_mismatch")
                    self._jwks_uri = conf["jwks_uri"]
                self._keys = self._parse(self._fetch(self._jwks_uri))
            except Unauthenticated:
                raise
            except Exception:
                raise Unauthenticated("idp_keys_unavailable")

    def _key_for(self, kid: str):
        if kid not in self._keys:
            self._refresh()  # key rotation: the IdP may have published a new key
        if kid not in self._keys:
            raise Unauthenticated("unknown_key")
        return self._keys[kid]

    # ---------- verify ----------
    def verify(self, token: str, now: float) -> str:
        jwt = self._jwt
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Unauthenticated("malformed")
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise Unauthenticated("bad_algorithm")
        key, pinned_alg = self._key_for(str(header.get("kid", "")))
        if pinned_alg and pinned_alg != alg:   # the JWK says which algorithm it is for
            raise Unauthenticated("bad_algorithm")
        try:
            # Signature, iss and aud here; time claims below, against the injected clock.
            claims = jwt.decode(token, key.key, algorithms=[alg], audience=self.audience, issuer=self.issuer,
                                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False,
                                         "require": ["exp", "iss", "aud", "sub"]})
        except jwt.InvalidSignatureError:
            raise Unauthenticated("bad_signature")
        except jwt.InvalidAudienceError:
            raise Unauthenticated("bad_audience")
        except jwt.InvalidIssuerError:
            raise Unauthenticated("bad_issuer")
        except jwt.PyJWTError:
            raise Unauthenticated("malformed")
        try:
            exp = float(claims["exp"])
            nbf = float(claims.get("nbf", now))
        except (TypeError, ValueError):
            raise Unauthenticated("malformed")
        if now > exp + self.leeway_s:
            raise Unauthenticated("expired")
        if nbf > now + self.leeway_s:
            raise Unauthenticated("not_yet_valid")

        subject = claims.get(self.subject_claim)
        if not isinstance(subject, str) or not subject.strip():
            raise Unauthenticated("no_subject")
        if self.subject_claim == "email" and claims.get("email_verified") is not True:
            raise Unauthenticated("email_not_verified")
        if self.allowed_domains is not None:
            email = subject if self.subject_claim == "email" else claims.get("email", "")
            if not isinstance(email, str) or email.rsplit("@", 1)[-1].lower() not in self.allowed_domains \
                    or "@" not in email:
                raise Unauthenticated("domain_not_allowed")
        return subject.strip()
