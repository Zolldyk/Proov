"""Service → tier mapping for Proov's two registered CAP services.

Maps a CROO `service_id` to a logical tier (`"quick"` / `"deep"`) so the rest of the
app can branch on tier without hard-coding service IDs at each call site. The IDs come
from Story 1.1 registration; they can be overridden via env in case the agent is
re-registered (the dashboard mints a fresh `svc-new-...` id each time).

SDK-agnostic by design — NO `croo` import here, so it stays a pure lookup that tests
and the deliverable builder can use without touching the SDK.
"""

from __future__ import annotations

import os

# Live service IDs. NOTE: CROO mints a fresh id on (re)registration and switched its
# id format from `svc-new-<digits>` to UUIDs — the Story 1.1 `svc-new-...` ids are dead.
# Quick Check confirmed live 2026-06-21 via a real paid order (order 2c4ac135…).
QUICK_SERVICE_ID = "a31ee562-142f-44c8-88b9-a5991874792f"  # Quick Check — $0.10 / SLA 5m
# Both confirmed live 2026-06-21 from the dashboard. Override via env if re-registered.
DEEP_SERVICE_ID = "b8e4a546-69c4-42f5-b21f-087daa2333d0"  # Deep Verify — $0.50 / SLA 30m

_QUICK = "quick"
_DEEP = "deep"


def _quick_id() -> str:
    return os.environ.get("PROOV_QUICK_SERVICE_ID") or QUICK_SERVICE_ID


def _deep_id() -> str:
    return os.environ.get("PROOV_DEEP_SERVICE_ID") or DEEP_SERVICE_ID


def tier_for_service(service_id: str) -> str:
    """Return the tier (`"quick"`/`"deep"`) for `service_id`.

    Reads the (optionally env-overridden) known IDs on each call so a re-registration
    that updates the env takes effect without a restart-time snapshot. Unknown IDs
    default to `"quick"` — the happy path is permissive; strict validation lands in
    Story 1.5.
    """
    if service_id == _deep_id():
        return _DEEP
    if service_id == _quick_id():
        return _QUICK
    return _QUICK
