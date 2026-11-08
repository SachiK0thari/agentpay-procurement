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
