import os

from agentpay.config import load_dotenv


def test_dotenv_parsing_and_precedence(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nSTRIPE_SECRET_KEY=sk_test_abc\nexport SESSION_SECRET='quoted value'\n"
        'BASE_URL="http://x:8000"  # not a comment inside quotes\nAGENTPAY_DEV_LOGIN=1 # trailing comment\n'
        "ALREADY_SET=from_file\nmalformed line\n"
    )
    monkeypatch.setenv("ALREADY_SET", "from_shell")
    for k in ("STRIPE_SECRET_KEY", "SESSION_SECRET", "BASE_URL", "AGENTPAY_DEV_LOGIN"):
        monkeypatch.delenv(k, raising=False)
    loaded = load_dotenv(f)
    assert loaded == {"STRIPE_SECRET_KEY": "sk_test_abc", "SESSION_SECRET": "quoted value",
                      "BASE_URL": "http://x:8000", "AGENTPAY_DEV_LOGIN": "1"}
    assert os.environ["ALREADY_SET"] == "from_shell"      # shell wins
    assert load_dotenv(f, override=True)["ALREADY_SET"] == "from_file"


def test_missing_file_is_a_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope") == {}


def test_seed_budgets_sets_this_and_next_month_and_keeps_spend():
    import calendar

    from agentpay.app import seed_budgets
    from agentpay.store import Store

    s = Store()
    dec_31 = calendar.timegm((2026, 12, 31, 23, 0, 0))
    seed_budgets(s, "it=500000; eng=200000", dec_31)
    assert s.budget("it", "2026-12")["limit_cents"] == 500_000
    assert s.budget("it", "2027-01")["limit_cents"] == 500_000     # year rollover
    assert s.budget("eng", "2027-01")["limit_cents"] == 200_000
    s.db.execute("UPDATE budgets SET spent_cents = 1234 WHERE team = 'it' AND period = '2026-12'")
    seed_budgets(s, "it=600000", dec_31)                            # restart with a new limit
    assert s.budget("it", "2026-12")["limit_cents"] == 600_000
    assert s.budget("it", "2026-12")["spent_cents"] == 1234         # spend survives a restart


def test_redact_masks_stripe_secrets_and_approval_tokens():
    from agentpay.config import redact
    line = ('Invalid API Key provided: sk_test_51AbcDEF123 whsec_abcXYZ987 rk_live_q1w2 '
            '"POST /approvals/int_1.approver_it.ab12.1794772047.deadbeef/approve HTTP/1.1"')
    out = redact(line)
    for leaked in ("51AbcDEF123", "abcXYZ987", "q1w2", "deadbeef", "ab12"):
        assert leaked not in out
    assert "sk_test_****" in out and "/approvals/<token>/approve" in out


def test_redacting_filter_keeps_uvicorn_access_args_positional():
    import logging

    from agentpay.config import RedactingFilter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:1", "POST", "/approvals/int_1.a.b.1.sig/approve", "1.1", 200), None)
    assert RedactingFilter().filter(rec)
    client, method, path, ver, status = rec.args            # what AccessFormatter unpacks
    assert path == "/approvals/<token>/approve" and status == 200


def test_redacting_filter_scrubs_tracebacks():
    import logging
    import sys

    from agentpay.config import RedactingFilter
    try:
        raise RuntimeError("Invalid API Key provided: sk_test_51SECRETVALUE")
    except RuntimeError:
        rec = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    RedactingFilter().filter(rec)
    assert rec.exc_info is None and "SECRETVALUE" not in rec.exc_text and "sk_test_****" in rec.exc_text
