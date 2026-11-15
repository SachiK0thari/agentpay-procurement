"""Load a `.env` file into os.environ for local development.

No dependency on python-dotenv: the format accepted is one KEY=VALUE per line, `#` comments,
blank lines, optional `export ` prefix, optional single or double quotes around the value.
Variables already set in the environment win, so a real deployment that injects secrets
through its platform is unaffected by a stray file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env", *, override: bool = False) -> dict[str, str]:
    """Returns the variables that were set (not the ones skipped because already present)."""
    p = Path(path)
    if not p.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            close = value.find(value[0], 1)
            value = value[1:close] if close > 0 else value[1:]   # quoted: keep as-is, drop trailing comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()             # unquoted: strip trailing comment
        if not key or (key in os.environ and not override):
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


# Stripe keys / webhook secrets, and approval tokens (`<intent>.<approver>.<nonce>.<exp>.<hmac>`).
_SECRET_RE = re.compile(r"\b((?:sk|rk|pk)_(?:test|live)_|whsec_)[A-Za-z0-9*]+")
_TOKEN_RE = re.compile(r"(/approvals/)[^/\s\"']+")


def redact(text: str) -> str:
    """Mask credentials before text reaches a log line, an HTTP error body, or a terminal."""
    return _TOKEN_RE.sub(r"\1<token>", _SECRET_RE.sub(r"\1****", str(text)))


class RedactingFilter:
    """logging filter: applies redact() to the message, its args, and any traceback."""

    def filter(self, record) -> bool:
        if isinstance(record.args, tuple) and record.args:
            # Redact each arg in place: uvicorn's AccessFormatter unpacks record.args by position.
            record.msg = redact(record.msg)
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        else:
            msg = record.getMessage()
            clean = redact(msg)
            if clean != msg:
                record.msg, record.args = clean, None
        if record.exc_info:  # tracebacks carry exception messages too (e.g. Stripe auth errors)
            import traceback
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
            record.exc_info = None
        return True
