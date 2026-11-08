"""Load a `.env` file into os.environ for local development.

No dependency on python-dotenv: the format accepted is one KEY=VALUE per line, `#` comments,
blank lines, optional `export ` prefix, optional single or double quotes around the value.
Variables already set in the environment win, so a real deployment that injects secrets
through its platform is unaffected by a stray file.
"""
from __future__ import annotations

import os
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
