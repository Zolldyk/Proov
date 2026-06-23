"""Tests for proov.config.AppConfig — .env parsing, env precedence, fail-fast, secrecy."""

from __future__ import annotations


import pytest

from proov.config import AppConfig, ConfigError

_VARS = ("CROO_API_URL", "CROO_WS_URL", "CROO_API_KEY", "CROO_REQUESTER_API_KEY")
_SECRET = "croo_sk_supersecret_value_should_never_leak"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure each test starts with the relevant vars unset and restores after."""
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def _write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_from_env_parses_quotes_comments_and_blanks(tmp_path):
    env = _write_env(
        tmp_path,
        "\n".join(
            [
                "# a comment",
                "",
                'CROO_API_URL="https://api.croo.network"',
                "CROO_WS_URL='wss://api.croo.network/ws'",
                f"CROO_API_KEY={_SECRET}",
                "   # indented comment",
                "CROO_REQUESTER_API_KEY=croo_sk_requester",
            ]
        ),
    )
    cfg = AppConfig.from_env(env_file=env)

    assert cfg.api_url == "https://api.croo.network"  # double quotes stripped
    assert cfg.ws_url == "wss://api.croo.network/ws"  # single quotes stripped
    assert cfg.api_key == _SECRET
    assert cfg.requester_api_key == "croo_sk_requester"


def test_real_env_is_not_overwritten_by_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("CROO_API_URL", "https://real.example")
    env = _write_env(
        tmp_path,
        "\n".join(
            [
                "CROO_API_URL=https://dotenv.example",
                "CROO_WS_URL=wss://api.croo.network/ws",
                f"CROO_API_KEY={_SECRET}",
            ]
        ),
    )
    cfg = AppConfig.from_env(env_file=env)

    # Pre-set process env wins over the .env value.
    assert cfg.api_url == "https://real.example"


@pytest.mark.parametrize("missing", ["CROO_API_URL", "CROO_WS_URL", "CROO_API_KEY"])
def test_missing_required_var_fails_fast_and_names_it(tmp_path, missing):
    present = {
        "CROO_API_URL": "https://api.croo.network",
        "CROO_WS_URL": "wss://api.croo.network/ws",
        "CROO_API_KEY": _SECRET,
    }
    present.pop(missing)
    env = _write_env(tmp_path, "\n".join(f"{k}={v}" for k, v in present.items()))

    with pytest.raises(ConfigError) as excinfo:
        AppConfig.from_env(env_file=env)

    assert missing in str(excinfo.value)


def test_exception_message_never_contains_the_key(tmp_path):
    # Key present but ws_url missing -> error must not echo the secret key.
    env = _write_env(
        tmp_path,
        "\n".join(["CROO_API_URL=https://api.croo.network", f"CROO_API_KEY={_SECRET}"]),
    )
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.from_env(env_file=env)

    assert _SECRET not in str(excinfo.value)


def test_missing_env_file_is_a_noop(tmp_path, monkeypatch):
    # No .env file; vars come straight from the process environment.
    monkeypatch.setenv("CROO_API_URL", "https://api.croo.network")
    monkeypatch.setenv("CROO_WS_URL", "wss://api.croo.network/ws")
    monkeypatch.setenv("CROO_API_KEY", _SECRET)

    cfg = AppConfig.from_env(env_file=tmp_path / "does-not-exist.env")
    assert cfg.api_key == _SECRET
    assert cfg.requester_api_key is None  # optional, absent
