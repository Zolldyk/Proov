"""Secret redaction for logs (NFR5 secret hygiene).

The CROO SDK's own `croo` logger emits the WebSocket connection URL *including the API
key* (`...?key=croo_sk_...`) at INFO level. We deliberately enable that logger so the
SDK's reconnect/heartbeat messages surface — so we attach this filter to scrub any key
material before a record is emitted. Defense in depth: our own code never logs the key,
and this guarantees the SDK can't leak it either (terminal scrollback, log files, etc.).
"""

from __future__ import annotations

import logging
import re

_REDACTED = "[REDACTED]"

# `key=<token>` query param — stops at &, whitespace, or a quote.
_KEY_PARAM_RE = re.compile(r"(key=)[^&\s\"']+", re.IGNORECASE)
# Any bare `croo_sk_...` token. The charset is deliberately broad (covers base58/
# base64-ish keys with `+`, `/`, `=`, `.`) — stop only at whitespace, quotes, or `&`
# so a partial token can't leak its tail. Exact-value scrubbing (below) is the backstop.
_SK_TOKEN_RE = re.compile(r"croo_sk_[^\s\"'&]+")

# Known literal secret values to scrub verbatim, regardless of format. Registered via
# `register_secret()` (e.g. the resolved CROO_API_KEY) so even keys that don't match the
# `croo_sk_` shape are guaranteed to be redacted.
_LITERAL_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret value to be scrubbed verbatim from all log output."""
    if value:
        _LITERAL_SECRETS.add(value)


def redact(text: str) -> str:
    """Return `text` with any CROO key material replaced by `[REDACTED]`."""
    for secret in _LITERAL_SECRETS:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    text = _KEY_PARAM_RE.sub(r"\1" + _REDACTED, text)
    text = _SK_TOKEN_RE.sub("croo_sk_" + _REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs API keys from each record's rendered message.

    Covers the formatted message, its args, and any exception/stack text attached to
    the record (a key inside a traceback is a real leak vector). Fails *closed*: if
    anything goes wrong it scrubs the raw `record.msg` rather than emitting unredacted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # never block logging on a redaction error
            # Fail closed: best-effort scrub of the raw template so a key in record.msg
            # can't slip through even when arg-formatting fails.
            with _suppress():
                record.msg = redact(str(record.msg))
                record.args = ()
            return True
        redacted = redact(message)
        if redacted != message:
            # Replace the message and drop args so re-formatting can't re-expand the key.
            record.msg = redacted
            record.args = ()

        # Exception / stack text are rendered separately by the formatter — scrub them too.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class _suppress:
    """Tiny contextlib.suppress(Exception) without importing contextlib here."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)


def install_secret_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the redacting filter to every handler of `logger` (root by default).

    Filters live on the *handlers* (not the logger) so they also apply to records
    propagated up from child loggers such as `croo`. As defense-in-depth (in case the
    SDK ever attaches its own non-propagating handler), the filter is also attached to
    the `croo` logger directly. Idempotent.
    """
    target = logger if logger is not None else logging.getLogger()
    targets = [target]
    if logger is None:
        targets.append(logging.getLogger("croo"))
    for tgt in targets:
        if not any(isinstance(f, SecretRedactingFilter) for f in tgt.filters):
            tgt.addFilter(SecretRedactingFilter())
        for handler in tgt.handlers:
            if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
                handler.addFilter(SecretRedactingFilter())
