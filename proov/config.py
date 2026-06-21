"""Application configuration: load required CROO settings from the environment.

The CROO Python SDK reads NO environment variables — it takes config explicitly
(`Config(base_url, ws_url)` + `AgentClient(config, sdk_key=...)`). This module is the
one place that reads `.env`/`os.environ` and produces an `AppConfig` the rest of the
app passes around. It stays SDK-agnostic (no `croo` import here).

Secret hygiene: the API key value is never logged and never placed in an exception
message — only the *names* of missing variables are reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Required variables, in the order we report a missing one (fail-fast on the first gap).
_REQUIRED = ("CROO_API_URL", "CROO_WS_URL", "CROO_API_KEY")


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    """Resolved runtime configuration for the provider process."""

    api_url: str
    ws_url: str
    api_key: str
    # Used from Story 1.3+ (test buyer places orders); optional here.
    requester_api_key: str | None = None

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = ".env") -> "AppConfig":
        """Build an AppConfig from `.env` (if present) overlaid with the process env.

        Loads `env_file` via a tiny built-in parser (no `python-dotenv` dependency),
        without overwriting variables already set in `os.environ`, then reads the
        required vars. Raises `ConfigError` naming the FIRST missing required var.
        """
        if env_file is not None:
            _load_dotenv(env_file)

        missing = [name for name in _REQUIRED if not os.environ.get(name)]
        if missing:
            # Report only the variable name — never any value.
            raise ConfigError(f"Missing required environment variable: {missing[0]}")

        return cls(
            api_url=os.environ["CROO_API_URL"],
            ws_url=os.environ["CROO_WS_URL"],
            api_key=os.environ["CROO_API_KEY"],
            requester_api_key=os.environ.get("CROO_REQUESTER_API_KEY") or None,
        )


def _load_dotenv(env_file: str | os.PathLike[str]) -> None:
    """Minimal `.env` loader.

    Reads `env_file` if it exists; ignores blank lines and `#` comments; splits each
    line on the FIRST `=`; strips surrounding single/double quotes from the value; and
    does NOT overwrite a variable already present in `os.environ` (real env wins).
    A missing file is a no-op.
    """
    path = Path(env_file)
    if not path.is_file():
        return

    # utf-8-sig strips a leading BOM if present, so the first key isn't read as
    # "﻿CROO_API_URL" (which would otherwise fail fast as a "missing" var).
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Tolerate the common `export KEY=val` shell form.
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export"):].strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value
