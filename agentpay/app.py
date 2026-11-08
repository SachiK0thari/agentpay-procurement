"""HTTP surface (optional; needs `pip install -e .[server]`).

Run:   uvicorn agentpay.app:app --reload
Forward test-mode webhooks:
       stripe listen --forward-to localhost:8000/webhooks/stripe

Endpoints
  POST /webhooks/stripe              signed Stripe events; real-time auth decision in the body
  POST /auth/dev-login               {approver_id} -> session cookie (only if AGENTPAY_DEV_LOGIN=1)
  POST /intents                      submit a PurchaseIntent (requester = session subject)
  GET  /intents/{id}                 state + audit trail
  GET  /approvals/{token}            approval page (what, who, why, approve / reject)
  POST /approvals/{token}/{decision} approve | reject; approver = session subject, never a param
  POST /tasks/housekeeping           expire stale approvals, sweep unused cards
  GET  /healthz

`create_app` wires explicit dependencies so tests run it against fakes;
`app_from_env` is what uvicorn loads.
"""
from __future__ import annotations

import html
import logging
import os
import time
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .approvals import ApprovalSigner, InvalidApproval
from .config import load_dotenv
from .auth import COOKIE_NAME, Authenticator, SessionAuth, Unauthenticated
from .executor import Executor
from .models import PurchaseIntent
from .policy import PolicyConfig
from .risk import RiskConfig
from .service import ProcurementService
from .store import Store
from .stripe_gateway import LiveStripe, TransientStripeError
from .webhooks import InvalidSignature, WebhookHandler

log = logging.getLogger("agentpay")


def create_app(service: ProcurementService, webhooks: WebhookHandler, store: Store, auth: Authenticator,
               *, stripe_version: str, now: Callable[[], float] = time.time,
               session_issuer: Optional[SessionAuth] = None, dev_login: bool = False,
               base_url: str = "") -> FastAPI:
    app = FastAPI(title="agentpay")

    def current_user(request: Request, authorization: Optional[str] = Header(default=None)) -> str:
        token = request.cookies.get(COOKIE_NAME)
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            raise HTTPException(401, "not signed in")
        try:
            return auth.verify(token, now())
        except Unauthenticated as e:
            raise HTTPException(401, f"session {e}")

    # ---------- Stripe ----------
    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request, stripe_signature: str = Header(...)):
        try:
            body = webhooks.handle(await request.body(), stripe_signature)
        except InvalidSignature:
            raise HTTPException(400, "invalid signature")
        except TransientStripeError as e:
            # Tell Stripe to retry the event; our handlers are idempotent.
            raise HTTPException(503, f"stripe unavailable: {e}")
        if "approved" in body:  # real-time authorization decision
            if webhooks.last_decision:
                log.info("realtime auth approved=%s reasons=%s", *webhooks.last_decision)
            return JSONResponse(body, headers={"Stripe-Version": stripe_version})
        return body

    # ---------- auth ----------
    if dev_login:
        if session_issuer is None:
            raise ValueError("dev_login needs a session_issuer")

        @app.post("/auth/dev-login")
        def dev_login_endpoint(payload: dict):
            subject = str(payload.get("approver_id") or payload.get("user_id") or "").strip()
            if not subject:
                raise HTTPException(400, "approver_id required")
            token = session_issuer.issue(subject, now())
            resp = JSONResponse({"subject": subject, "token": token})
            resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
            return resp

    # ---------- intents ----------
    @app.post("/intents")
    def submit_intent(payload: dict, user: str = Depends(current_user)):
        payload = dict(payload, user_id=user)  # the requester is whoever is signed in
        payload.pop("provenance", None)        # provenance is assigned by code, never by the client
        try:
            intent = PurchaseIntent.from_dict(payload)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"bad intent: {e}")
        try:
            r = service.submit(intent)
        except TransientStripeError as e:
            raise HTTPException(503, f"stripe unavailable: {e}")
        return {"intent_id": r.intent_id, "state": r.state.value, "reasons": r.reasons,
                "approval_url": f"{base_url}/approvals/{r.approval_token}" if r.approval_token else None}

    @app.get("/intents/{intent_id}")
    def get_intent(intent_id: str, user: str = Depends(current_user)):
        try:
            rec = store.get(intent_id)
        except KeyError:
            raise HTTPException(404, "unknown intent")
        return {"state": rec.state.value, "amount": rec.intent.amount, "card_id": rec.card_id,
                "intent": rec.intent.to_dict(), "audit": store.audit_log(intent_id)}

    # ---------- approvals ----------
    @app.get("/approvals/{token}", response_class=HTMLResponse)
    def approval_page(token: str, request: Request):
        try:
            user = current_user(request, request.headers.get("authorization"))
        except HTTPException:
            return HTMLResponse(_page("Sign in required",
                                      "<p>Open this link from a signed-in session.</p>"), status_code=401)
        try:
            claim = service.signer.verify(token, now())
            rec = store.get(claim.intent_id)
        except (InvalidApproval, KeyError) as e:
            return HTMLResponse(_page("Invalid approval link", f"<p>{html.escape(str(e))}</p>"), status_code=403)
        return HTMLResponse(_approval_html(token, user, rec, store.audit_log(claim.intent_id)))

    @app.post("/approvals/{token}/{decision}")
    def decide(token: str, decision: str, user: str = Depends(current_user)):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "decision must be approve or reject")
        fn = service.approve if decision == "approve" else service.reject
        try:
            r = fn(token, user)
        except InvalidApproval as e:
            raise HTTPException(403, str(e))
        except TransientStripeError as e:
            raise HTTPException(503, f"stripe unavailable: {e}")
        return {"intent_id": r.intent_id, "state": r.state.value, "decided_by": user}

    # ---------- ops ----------
    @app.post("/tasks/housekeeping")
    def housekeeping(user: str = Depends(current_user)):
        return service.housekeeping()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


# ---------------------------------------------------------------- HTML
def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--ok:#1b7f3b;--bad:#b3261e;--chip:#f3f3f3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#aaa;--line:#333;--chip:#222}}}}
body{{margin:0;padding:24px 16px;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto}} h1{{font-size:1.4rem;margin:0 0 4px}} .muted{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;margin:16px 0}} td,th{{text-align:left;padding:8px 4px;border-bottom:1px solid var(--line)}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.chip{{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 10px;margin:2px;font-size:.85rem}}
.row{{display:flex;gap:12px;margin-top:20px}} button{{flex:1;padding:12px;font-size:1rem;border:0;border-radius:8px;color:#fff;cursor:pointer}}
.ok{{background:var(--ok)}} .bad{{background:var(--bad)}} #out{{margin-top:16px;font-weight:600}}
</style></head><body><main>{body}</main></body></html>"""


def _approval_html(token: str, user: str, rec, audit: list[dict]) -> str:
    it = rec.intent
    rows = "".join(
        f"<tr><td>{html.escape(i.sku)}</td><td>{html.escape(i.description)}</td>"
        f"<td class=num>{i.quantity}</td><td class=num>${i.unit_amount / 100:,.2f}</td>"
        f"<td class=num>${i.quantity * i.unit_amount / 100:,.2f}</td></tr>" for i in it.items)
    reasons = {r for e in audit for r in e["reasons"]
               if not r.startswith(("card:", "auth_", "approved_by", "rejected_by"))}
    chips = "".join(f"<span class=chip>{html.escape(r)}</span>" for r in sorted(reasons)) or "<span class=muted>none</span>"
    prov = "".join(f"<span class=chip>{html.escape(k)}: {html.escape(v.value)}</span>" for k, v in it.provenance.items())
    t = html.escape(token)
    body = f"""
<h1>Purchase approval</h1>
<p class=muted>Signed in as <b>{html.escape(user)}</b> · intent <code>{html.escape(it.intent_id)}</code> · state <b>{html.escape(rec.state.value)}</b></p>
<table><tr><th>Requested by</th><td>{html.escape(it.user_id)} (team {html.escape(it.team)})</td></tr>
<tr><th>Vendor</th><td>{html.escape(it.vendor)} <span class=muted>({html.escape(it.mcc)})</span></td></tr>
<tr><th>Justification</th><td>{html.escape(it.justification) or '<span class=muted>none given</span>'}</td></tr>
<tr><th>Total</th><td><b>${it.amount / 100:,.2f}</b> {html.escape(it.currency.upper())}</td></tr></table>
<table><tr><th>SKU</th><th>Item</th><th class=num>Qty</th><th class=num>Unit</th><th class=num>Line</th></tr>{rows}</table>
<p><b>Why this needs you:</b> {chips}</p>
<p><b>Where the values came from:</b> {prov}</p>
<div class=row><button class=ok onclick="go('approve')">Approve — issue a ${it.amount / 100:,.2f} single-use card</button>
<button class=bad onclick="go('reject')">Reject</button></div>
<div id=out role=status></div>
<script>
async function go(d){{const o=document.getElementById('out');o.textContent='…';
const r=await fetch('/approvals/{t}/'+d,{{method:'POST',credentials:'same-origin'}});
const j=await r.json().catch(()=>({{}}));
o.textContent=r.ok?('Done: '+j.state):('Refused: '+(j.detail||r.status));
document.querySelectorAll('button').forEach(b=>b.disabled=true);}}
</script>"""
    return _page("Purchase approval", body)


# ---------------------------------------------------------------- env wiring
def app_from_env() -> FastAPI:
    import stripe  # server extra

    load_dotenv(os.environ.get("AGENTPAY_ENV_FILE", ".env"))
    store = Store(os.environ.get("AGENTPAY_DB", "agentpay.db"))
    gateway = LiveStripe(os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_CARDHOLDER_ID"])
    session = SessionAuth(os.environ["SESSION_SECRET"].encode())
    base_url = os.environ.get("BASE_URL", "http://localhost:8000")
    vendors = {v: set(m.split("|")) for v, m in
               (pair.split("=") for pair in os.environ.get("VENDORS", "Dell=computer_software_stores").split(";"))}
    service = ProcurementService(
        store=store,
        policy_cfg=PolicyConfig(vendors=vendors),
        risk_cfg=RiskConfig(trusted_vendors=set(os.environ.get("TRUSTED_VENDORS", "").split(",")) - {""}),
        signer=ApprovalSigner(os.environ["APPROVAL_SECRET"].encode()),
        executor=Executor(store, gateway),
        approvers={"it": os.environ.get("IT_APPROVER", "approver_it")},
        now=time.time,
        notifier=lambda iid, token, reasons: log.warning(
            "APPROVAL NEEDED %s reasons=%s -> %s/approvals/%s", iid, reasons, base_url, token),
    )
    webhooks = WebhookHandler(store, gateway, os.environ["STRIPE_WEBHOOK_SECRET"], now=time.time)
    return create_app(service, webhooks, store, session,
                      stripe_version=os.environ.get("STRIPE_API_VERSION", stripe.api_version),
                      session_issuer=session, dev_login=os.environ.get("AGENTPAY_DEV_LOGIN") == "1",
                      base_url=base_url)


# uvicorn agentpay.app:app — built only when the environment (or a .env file) is configured, so
# that importing this module in tests (which call create_app directly) needs no secrets.
load_dotenv(os.environ.get("AGENTPAY_ENV_FILE", ".env"))
app = app_from_env() if os.environ.get("STRIPE_SECRET_KEY") else None
