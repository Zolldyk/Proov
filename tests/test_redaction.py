"""Tests for proov.redaction — the SDK key must never survive into log output."""

from __future__ import annotations

import io
import logging

from proov.redaction import (
    SecretRedactingFilter,
    install_secret_redaction,
    redact,
)

_KEY = "croo_sk_cf5b706d9abf30dc9f27622b50cac32e"


def test_redact_scrubs_key_query_param():
    url = f"websocket connecting: wss://api.croo.network/ws?key={_KEY}"
    out = redact(url)
    assert _KEY not in out
    assert "key=[REDACTED]" in out


def test_redact_scrubs_bare_token_anywhere():
    out = redact(f"using sdk_key {_KEY} now")
    assert _KEY not in out
    assert "croo_sk_[REDACTED]" in out


def test_redact_leaves_clean_text_untouched():
    text = "websocket reconnected"
    assert redact(text) == text


def test_filter_mutates_record_message():
    record = logging.LogRecord(
        name="croo",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="websocket connecting: wss://api.croo.network/ws?key=%s",
        args=(_KEY,),
        exc_info=None,
    )
    assert SecretRedactingFilter().filter(record) is True
    assert _KEY not in record.getMessage()


def test_install_redaction_scrubs_emitted_log_line():
    # Simulate the exact SDK INFO line reaching a stream handler.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("test.croo.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    install_secret_redaction(logger)
    # Idempotent: a second install must not stack a duplicate filter.
    install_secret_redaction(logger)
    assert sum(isinstance(f, SecretRedactingFilter) for f in handler.filters) == 1

    logger.info("websocket connecting: wss://api.croo.network/ws?key=%s", _KEY)
    output = stream.getvalue()
    assert _KEY not in output
    assert "key=[REDACTED]" in output
