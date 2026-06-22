"""Deterministic verdict aggregation (FR10) — the PURE, SYNCHRONOUS engine slice.

Unlike `proov/llm.py` / `proov/search.py` / `proov/citations.py` (async network I/O with
timeouts and injectable clients), this module is **pure deterministic computation** — its
structural template is `proov/receipt.py`, not `search.py`. There is NO `croo`, NO `httpx`,
NO `async`, NO network, NO clock/`time`/`datetime`, NO randomness, NO env reads. It rolls
the per-claim judgments (Story 2.3) and per-source citation checks (Story 2.4) up into one
aggregate `Verdict` (`pass`/`fail`/`partial` + an overall confidence + the PRD §6 `stats`
counts).

**Determinism is load-bearing.** Story 2.6 embeds the resulting `verdict` + `confidence`
into the report body, and CAP anchors `keccak256(canonical_json(deliverable))` on Base
(architecture §5); a verifier re-hashes to confirm tamper-evidence. So `aggregate_verdict`
MUST be a pure mathematical function of its inputs — same inputs ⇒ same bytes on CPython.
The label uses only commutative `any()`/counting (order-independent); the confidence mean
iterates `judgments` in the given list order so its `sum` is reproducible.

The rule reads **`Judgment.status`** (the already-precision-calibrated 2.3 label), NOT the
raw `Judgment.evidence` stances — the per-claim status is authoritative (see Story 2.5 Dev
Notes; this consciously closes the deferred-work 2.3 status/stance item).
"""

from __future__ import annotations

import logging
import math

from .types import CitationCheck, Judgment, Verdict, VerdictLabel, clamp_confidence

# Used ONLY on the defensive unknown-status branch below (logging is the one permitted
# side-effect, matching `search.py`/`citations.py`). It is NOT touched on the happy path.
logger = logging.getLogger("proov.verdict")


def aggregate_verdict(
    judgments: list[Judgment],
    citations: list[CitationCheck],
    *,
    options: dict | None = None,
) -> Verdict:
    """Roll per-claim judgments + citation checks into one deterministic `Verdict` (FR10).

    The label is computed by these EXACT rules in this precedence order (a `fail` trigger
    beats everything):
      - `fail`    ⟸ any `citation.flag == "fabricated"` OR any `judgment.status == "unsupported"`
                    (FR10: `fail` = ≥1 fabricated citation or an unsupported/refuted claim).
      - `partial` ⟸ (not fail) AND (`claims_total == 0` OR any `judgment.status == "unverifiable"`)
                    (architecture §4: `partial` = "otherwise, some unverifiable, none refuted";
                    zero judged claims can NEVER be `pass` — nothing was positively verified).
      - `pass`    ⟸ otherwise: ≥1 claim, every claim `supported`, no `fabricated` citation,
                    no `unverifiable` claim (FR10 refined by architecture §4's parenthetical:
                    any `unverifiable` claim demotes to `partial`, never rides a clean `pass`).

    `misattributed` (and `ok`) citations have NO effect on the label in v1 — the literal-FR10
    reading (Story 2.5 Open Question 1); only `fabricated` gates the verdict.

    Total and defensive (degrade, don't drop — NFR3): empty `judgments`/`citations` are valid;
    an unrecognised `status` is counted as `unverifiable` (the precision-safe bucket); an
    unrecognised `flag` is treated as non-fabricated. It never raises out (no I/O to fail) and
    is not wrapped in try/except — the defensiveness is structural, not exception-swallowing.

    `options` is accepted for forward-compat (the Story 2.7 Deep seam, like the other engine
    entrypoints) and is unused in v1.
    """
    claims_total = len(judgments)
    supported = 0
    unsupported = 0
    unverifiable = 0
    conf_terms: list[float] = []

    for j in judgments:
        status = j.status
        if status == "supported":
            supported += 1
        elif status == "unsupported":
            unsupported += 1
        elif status == "unverifiable":
            unverifiable += 1
        else:
            # Defensive guard against a misbehaving pluggable provider: an unknown status is
            # bucketed as `unverifiable` (the precision-safe choice — never promote to
            # `supported`, and never let it trigger the `fail` path).
            unverifiable += 1
            logger.warning(
                "aggregate_verdict: unknown claim status %r counted as unverifiable", status
            )
        # Clamp each term so a single bad/`nan`/out-of-range confidence cannot poison the mean
        # (keeps every term finite and in `[0, 1]`).
        conf_terms.append(clamp_confidence(j.confidence))

    # Exact match only — any other/unknown flag is non-fabricated (never cry `fail` on an
    # unknown flag). `misattributed`/`ok` deliberately do not feed this (OQ1).
    has_fabricated = any(c.flag == "fabricated" for c in citations)

    if has_fabricated or unsupported > 0:
        # FR10: ≥1 fabricated citation OR an unsupported (refuted) claim. Highest precedence.
        label: VerdictLabel = "fail"
    elif claims_total == 0 or unverifiable > 0:
        # architecture §4: zero claims (nothing positively verified) or any unverifiable claim
        # → `partial`. Precision over recall — never assert `pass` on an empty/uncertain run.
        label = "partial"
    else:
        # FR10: ≥1 claim, all supported, no fabricated citation, no unverifiable claim.
        label = "pass"

    # `math.fsum` is exact-rounding and order-independent, so the mean — and the bytes it
    # hashes to in the on-chain receipt (Story 2.6) — are reproducible regardless of the
    # judgment ordering. Always routed through `clamp_confidence` so it is a `float` in
    # `[0, 1]` (`0.0`, not int `0`) that canonicalises cleanly into the hashed report body.
    # v1 is the mean of per-claim confidences; calibration (evidence agreement + coverage)
    # is Story 3.1.
    confidence = (
        clamp_confidence(math.fsum(conf_terms) / claims_total)
        if claims_total
        else clamp_confidence(0.0)
    )

    return Verdict(
        label=label,
        confidence=confidence,
        claims_total=claims_total,
        supported=supported,
        unsupported=unsupported,
        unverifiable=unverifiable,
    )
