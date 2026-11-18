"""OIDCAuth: tokens from the organisation's identity provider decide who the approver is."""
import base64
import json

import pytest

jwt = pytest.importorskip("jwt")  # `pip install -e ".[sso]"`
pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402
from jwt.algorithms import ECAlgorithm, RSAAlgorithm  # noqa: E402

from agentpay.auth import OIDCAuth, SessionAuth, Unauthenticated  # noqa: E402

ISS = "https://idp.example.com"
AUD = "agentpay-client"
NOW = 1_792_500_000.0


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = rsa_key()
OTHER = rsa_key()


def jwk(private, kid, alg=None):
    pub = private.public_key()
    d = json.loads((ECAlgorithm if isinstance(pub, ec.EllipticCurvePublicKey) else RSAAlgorithm).to_jwk(pub))
    d.update(kid=kid, use="sig")
    if alg:
        d["alg"] = alg
    return d


def token(key=KEY, kid="k1", alg="RS256", **claims):
    body = {"iss": ISS, "aud": AUD, "sub": "00u123", "email": "bob@acme.com", "email_verified": True,
            "iat": int(NOW), "exp": int(NOW) + 3600} | claims
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=alg, headers={"kid": kid})


def auth(**kw):
    kw.setdefault("jwks", {"keys": [jwk(KEY, "k1")]})
    return OIDCAuth(ISS, AUD, **kw)


def reason(a, tok, now=NOW):
    with pytest.raises(Unauthenticated) as e:
        a.verify(tok, now)
    return str(e.value)


# ---------------------------------------------------------------- happy paths
def test_valid_token_yields_subject():
    assert auth().verify(token(), NOW) == "00u123"
    assert auth(subject_claim="email").verify(token(), NOW) == "bob@acme.com"


def test_ec_keys_work():
    k = ec.generate_private_key(ec.SECP256R1())
    a = auth(jwks={"keys": [jwk(k, "ec1")]})
    assert a.verify(token(key=k, kid="ec1", alg="ES256"), NOW) == "00u123"


# ---------------------------------------------------------------- what must be refused
def test_signature_issuer_audience():
    a = auth()
    assert reason(a, token(key=OTHER)) == "bad_signature"           # right kid, wrong key
    assert reason(a, token(iss="https://evil.example.com")) == "bad_issuer"
    assert reason(a, token(aud="some-other-app")) == "bad_audience"
    assert reason(a, token()[:-6] + "AAAAAA") == "bad_signature"


def test_time_claims_use_the_injected_clock_with_leeway():
    a = auth(leeway_s=60)
    tok = token(exp=int(NOW) + 10)
    assert a.verify(tok, NOW + 60) == "00u123"                       # within leeway
    assert reason(a, tok, NOW + 71) == "expired"
    assert reason(a, token(nbf=int(NOW) + 600)) == "not_yet_valid"
    assert reason(a, token(exp=None)) == "malformed"                 # exp is required


def test_alg_none_is_refused():
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "kid": "k1"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"iss": ISS, "aud": AUD, "sub": "x",
                                                "exp": int(NOW) + 60}).encode()).rstrip(b"=").decode()
    assert reason(auth(), f"{header}.{body}.") == "bad_algorithm"


def test_hs256_signed_with_the_public_key_is_refused():
    """Algorithm confusion: an HMAC 'signature' keyed with the published RSA public key."""
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    import hashlib
    import hmac

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    signing_input = (b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode()) + "." +
                     b64(json.dumps({"iss": ISS, "aud": AUD, "sub": "mallory", "exp": int(NOW) + 60}).encode()))
    forged = signing_input + "." + b64(hmac.new(pem, signing_input.encode(), hashlib.sha256).digest())
    assert reason(auth(), forged) == "bad_algorithm"


@pytest.mark.parametrize("algs", [("HS256",), ("RS256", "none"), ()])
def test_symmetric_or_none_algorithms_cannot_be_configured(algs):
    with pytest.raises(ValueError):
        auth(algorithms=algs)


def test_jwk_pinned_algorithm_is_enforced():
    a = auth(jwks={"keys": [jwk(KEY, "k1", alg="RS512")]})
    assert reason(a, token(alg="RS256")) == "bad_algorithm"
    assert a.verify(token(alg="RS512"), NOW) == "00u123"


def test_email_subject_needs_verified_email_and_allowed_domain():
    a = auth(subject_claim="email", allowed_domains=["acme.com"])
    assert reason(a, token(email_verified=False)) == "email_not_verified"
    assert reason(a, token(email_verified=None)) == "email_not_verified"
    assert reason(a, token(email="bob@evil.com")) == "domain_not_allowed"
    assert reason(a, token(email="bob@acme.com.evil.com")) == "domain_not_allowed"
    assert reason(a, token(email=None)) == "no_subject"
    assert a.verify(token(email="Bob@ACME.com"), NOW) == "Bob@ACME.com"


def test_session_tokens_are_not_idp_tokens():
    s = SessionAuth(b"session-secret-0123456789").issue("bob", NOW)
    assert reason(auth(), s) == "malformed"


# ---------------------------------------------------------------- discovery and key rotation
class FakeIdP:
    def __init__(self, keys, issuer=ISS):
        self.keys, self.issuer, self.calls = keys, issuer, []

    def __call__(self, url):
        self.calls.append(url)
        if url == ISS + "/.well-known/openid-configuration":
            return {"issuer": self.issuer, "jwks_uri": ISS + "/keys"}
        if url == ISS + "/keys":
            return {"keys": list(self.keys)}
        raise OSError("404")


def test_discovery_then_rotation_with_rate_limited_refetch():
    idp = FakeIdP([jwk(KEY, "k1")])
    a = OIDCAuth(ISS, AUD, fetch=idp, refetch_interval_s=0)
    assert a.verify(token(), NOW) == "00u123"
    assert idp.calls == [ISS + "/.well-known/openid-configuration", ISS + "/keys"]
    idp.keys.append(jwk(OTHER, "k2"))                                # IdP rotates in a new key
    assert a.verify(token(key=OTHER, kid="k2"), NOW) == "00u123"
    assert idp.calls[-1] == ISS + "/keys" and len(idp.calls) == 3

    idp = FakeIdP([jwk(KEY, "k1")])
    a = OIDCAuth(ISS, AUD, fetch=idp, refetch_interval_s=300)
    a.verify(token(), NOW)
    for i in range(20):                                              # junk kids can't hammer the IdP
        assert reason(a, token(kid=f"junk{i}")) == "unknown_key"
    assert len(idp.calls) == 2


def test_discovery_issuer_mismatch_and_outage_fail_closed():
    assert reason(OIDCAuth(ISS, AUD, fetch=FakeIdP([], issuer="https://evil.example.com")), token()) \
        == "idp_issuer_mismatch"

    def down(url):
        raise OSError("connection refused")
    assert reason(OIDCAuth(ISS, AUD, fetch=down), token()) == "idp_keys_unavailable"


def test_default_fetch_refuses_plain_http():
    a = OIDCAuth("http://idp.example.com", AUD)
    assert reason(a, token()) == "idp_url_not_https"


# ---------------------------------------------------------------- through the HTTP app
def test_approval_over_http_with_idp_token(world):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentpay.app import create_app
    from agentpay.testing import make_intent

    a = auth()
    app = create_app(world.service, world.webhooks, world.store, a, stripe_version="v", now=world.clock,
                     user_teams={"*": "it"})
    c = TestClient(app)
    assert c.post("/auth/dev-login", json={"approver_id": "bob"}).status_code in (404, 405)
    r = world.service.submit(make_intent(60_000))
    now = int(world.clock())
    as_ = lambda sub: {"Authorization": "Bearer " + token(sub=sub, iat=now, exp=now + 600)}  # noqa: E731
    assert c.post(f"/approvals/{r.approval_token}/approve", headers=as_("alice")).status_code == 403
    forged = {"Authorization": "Bearer " + token(key=OTHER, sub="bob", iat=now, exp=now + 600)}
    assert c.post(f"/approvals/{r.approval_token}/approve", headers=forged).status_code == 401
    resp = c.post(f"/approvals/{r.approval_token}/approve", headers=as_("bob"))
    assert resp.status_code == 200 and resp.json()["decided_by"] == "bob" and resp.json()["state"] == "card_issued"


def test_email_approver_ids_work_end_to_end():
    """Review bug #3: approval tokens split on '.', so an email approver id (the recommended
    OIDC_SUBJECT_CLAIM=email setup) could never approve anything."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from agentpay.app import create_app
    from agentpay.testing import World, make_intent

    world = World()
    world.service.approvers["it"] = "bob.smith@corp.com"
    a = auth(subject_claim="email")
    app = create_app(world.service, world.webhooks, world.store, a, stripe_version="v", now=world.clock,
                     user_teams={"alice.jones@corp.com": "it"})
    c = TestClient(app)
    now = int(world.clock())
    as_ = lambda email: {"Authorization": "Bearer " + token(email=email, iat=now, exp=now + 600)}  # noqa: E731
    body = make_intent(60_000).to_dict()
    sub = c.post("/intents", json=body, headers=as_("alice.jones@corp.com")).json()
    assert sub["state"] == "pending_approval"
    tok = sub["approval_url"].rsplit("/", 1)[-1]
    assert c.get(f"/approvals/{tok}", headers=as_("bob.smith@corp.com")).status_code == 200
    resp = c.post(f"/approvals/{tok}/approve", headers=as_("bob.smith@corp.com"))
    assert resp.status_code == 200 and resp.json()["state"] == "card_issued"
    assert resp.json()["decided_by"] == "bob.smith@corp.com"


def test_auth_from_env():
    from agentpay.app import auth_from_env

    a, issuer, dev = auth_from_env({"OIDC_ISSUER": ISS, "OIDC_AUDIENCE": AUD, "OIDC_SUBJECT_CLAIM": "email",
                                    "OIDC_ALLOWED_DOMAINS": "acme.com, acme.co.uk"})
    assert isinstance(a, OIDCAuth) and issuer is None and dev is False
    assert a.subject_claim == "email" and a.allowed_domains == {"acme.com", "acme.co.uk"}
    with pytest.raises(ValueError, match="cannot be combined"):
        auth_from_env({"OIDC_ISSUER": ISS, "OIDC_AUDIENCE": AUD, "AGENTPAY_DEV_LOGIN": "1"})
    a, issuer, dev = auth_from_env({"SESSION_SECRET": "session-secret-0123456789", "AGENTPAY_DEV_LOGIN": "1"})
    assert isinstance(a, SessionAuth) and issuer is a and dev is True
