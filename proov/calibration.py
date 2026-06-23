"""Calibration scorer — measure verdict precision against a hand-labeled set (Story 3.1, NFR4).

This module makes the engine's precision **measurable, reproducible, and gated**. Its
structural template is `proov/verdict.py`, NOT `search.py`: the scoring math is **pure,
deterministic computation** — NO `croo`, NO `httpx`, NO network, NO clock/`time`, NO
randomness, NO env reads. Same inputs ⇒ same numbers on CPython. stdlib only (`dataclasses`,
`math`, `logging`); `json` is used **only** inside the thin `load_dataset` loader, kept apart
from the scoring functions.

What it computes (PRD §1 quality bar / NFR4 "a verifier that cries wolf is worse than
useless"): for each label a confusion matrix and `precision`/`recall`, and the **pooled
flagged-class precision** over the two verdict-flipping flags `{"unsupported", "fabricated"}`.
`meets_bar` keys on the ≥80% **precision** bar ONLY — recall is computed and reported but
NEVER gated (precision over recall: it is acceptable to miss a real bad claim; it is NOT
acceptable to falsely flag a good one). A class with **zero predictions** has *undefined*
precision (`None`, not `0.0`): it is excluded from the gate, never counted as a failure.

The one impure seam is `replay_dataset`, the bridge that turns the frozen `recorded_*` fields
of the committed dataset into predictions by feeding them through the REAL deterministic engine
code (`proov.llm._normalise_judgment`, `proov.citations._classify_retrievability`/`_flag_for`).
It imports those seams **lazily** (inside the function) so importing `proov.calibration` stays
stdlib-pure, and it is the SINGLE replay code path shared by `scripts/calibrate.py` and
`tests/test_calibration.py` (no duplication).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("proov.calibration")

# The two verdict-flipping classes the ≥80% bar gates: a per-claim `unsupported` judgment and
# a per-source `fabricated` citation flag (FR10 — both flip a verdict to `fail`). `supported`,
# `unverifiable`, `ok`, `misattributed` are measured/reported but do NOT gate the bar.
FLAG_LABELS: tuple[str, ...] = ("unsupported", "fabricated")

# The committed hand-labeled dataset (a product artifact, repo-relative, NOT gitignored —
# `.gitignore` ignores `*.db`/`*.sqlite`, not `.json`). Read by the loader below.
DEFAULT_DATASET_PATH = "calibration/calibration_set.json"


@dataclass(frozen=True)
class LabeledItem:
    """One scored row: the engine's `predicted` label vs the hand `gold` label.

    Frozen (immutable, hashable) in the pure-value style of `proov/types.py`. `predicted` and
    `gold` are bare label strings in the SAME space (`supported`/`unsupported`/`unverifiable`
    for claims, `ok`/`fabricated`/`misattributed` for citations); the two flag spaces are
    disjoint so they can be scored in one pass. `kind` is `"claim"` or `"citation"` (provenance
    only — the math is label-space agnostic). `thin_evidence` marks the rows whose evidence is
    empty/weak and MUST resolve to `unverifiable` (the anti-guess guard, asserted at 100%).
    """

    id: str
    predicted: str
    gold: str
    kind: str
    thin_evidence: bool = False


@dataclass(frozen=True)
class ClassMetrics:
    """The confusion matrix + precision/recall for ONE label.

    `precision`/`recall` are `None` when **undefined** (no predictions / no gold positives) —
    never `0.0`, so an unflagged class is excluded from the gate rather than failing it.
    """

    label: str
    tp: int
    fp: int
    fn: int
    predicted_total: int
    gold_total: int
    precision: float | None
    recall: float | None


@dataclass(frozen=True)
class CalibrationReport:
    """The full scoring result over a set of `LabeledItem`s.

    `per_class` maps every label seen to its `ClassMetrics`; `pooled_flag_precision` is the
    precision pooled across the two `FLAG_LABELS` (`None` when neither was predicted);
    `thin_total`/`thin_unverifiable` count the `thin_evidence` rows and how many predicted
    `unverifiable` (the anti-guess rate). `flag_labels` records the gated classes for the
    consumer (`meets_bar`, the report printer).
    """

    per_class: dict[str, ClassMetrics]
    pooled_flag_precision: float | None
    thin_total: int
    thin_unverifiable: int
    flag_labels: tuple[str, ...] = field(default=FLAG_LABELS)


def _ratio(numerator: int, denominator: int) -> float | None:
    """Exact rational `numerator/denominator`, or `None` when the denominator is 0 (undefined)."""
    if denominator == 0:
        return None
    return numerator / denominator


def score(items: list[LabeledItem], *, flag_labels: tuple[str, ...] = FLAG_LABELS) -> CalibrationReport:
    """Roll `LabeledItem`s into a `CalibrationReport` — PURE, deterministic, exact arithmetic.

    For every label that appears as a prediction OR a gold, computes a one-vs-rest confusion
    matrix: `tp` = predicted==label AND gold==label; `fp` = predicted==label AND gold!=label;
    `fn` = gold==label AND predicted!=label. `precision = tp/(tp+fp)` (`None` if no predictions
    of the class — undefined, excluded from the gate); `recall = tp/(tp+fn)` (`None` if no gold
    positives — reported, never gated). The **pooled flag precision** is
    `sum(tp over flag_labels) / sum(tp+fp over flag_labels)` (`None` if nothing was flagged).
    No I/O, no clock, no randomness — same inputs ⇒ same numbers.
    """
    labels = sorted({item.predicted for item in items} | {item.gold for item in items})

    per_class: dict[str, ClassMetrics] = {}
    for label in labels:
        tp = sum(1 for it in items if it.predicted == label and it.gold == label)
        fp = sum(1 for it in items if it.predicted == label and it.gold != label)
        fn = sum(1 for it in items if it.gold == label and it.predicted != label)
        predicted_total = sum(1 for it in items if it.predicted == label)
        gold_total = sum(1 for it in items if it.gold == label)
        per_class[label] = ClassMetrics(
            label=label,
            tp=tp,
            fp=fp,
            fn=fn,
            predicted_total=predicted_total,
            gold_total=gold_total,
            precision=_ratio(tp, tp + fp),
            recall=_ratio(tp, tp + fn),
        )

    pooled_tp = sum(per_class[label].tp for label in flag_labels if label in per_class)
    pooled_pred = sum(
        per_class[label].tp + per_class[label].fp for label in flag_labels if label in per_class
    )
    pooled_flag_precision = _ratio(pooled_tp, pooled_pred)

    thin_total = sum(1 for it in items if it.thin_evidence)
    thin_unverifiable = sum(
        1 for it in items if it.thin_evidence and it.predicted == "unverifiable"
    )

    return CalibrationReport(
        per_class=per_class,
        pooled_flag_precision=pooled_flag_precision,
        thin_total=thin_total,
        thin_unverifiable=thin_unverifiable,
        flag_labels=flag_labels,
    )


def meets_bar(report: CalibrationReport, threshold: float = 0.80) -> bool:
    """Does the report clear the precision bar? `True` iff pooled AND every flag class clear it.

    Keys on **precision only** (NFR4 precision-over-recall). The gate: pooled flag precision ≥
    `threshold` AND every flag class with a **defined** (non-`None`) precision ≥ `threshold`. A
    flag class with `None` precision — zero predictions — does NOT fail the gate (a verifier
    that flagged nothing `fabricated` has not failed precision; it simply made no such claim).
    If nothing at all was flagged (pooled is `None`), the bar is vacuously met.
    """
    pooled = report.pooled_flag_precision
    if pooled is not None and pooled < threshold:
        return False
    for label in report.flag_labels:
        metrics = report.per_class.get(label)
        if metrics is not None and metrics.precision is not None and metrics.precision < threshold:
            return False
    return True


def load_dataset(path: str | None = None) -> list[dict]:
    """Load the hand-labeled rows from the committed JSON dataset — a DUMB loader, no scoring.

    Reads `path` (default `DEFAULT_DATASET_PATH`, repo-relative) and returns the `rows` list.
    Tolerates either a bare top-level list or a `{"rows": [...]}` object (the committed file
    uses the object form so it can carry a header/`expected` note alongside the rows). No engine
    import, no scoring — kept separate from the pure math (the one `json` use in this module).
    """
    target = path if path is not None else DEFAULT_DATASET_PATH
    with open(target, encoding="utf-8") as handle:
        data = json.load(handle)
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"calibration dataset {target!r} has no 'rows' list")
    return rows


def replay_dataset(rows: list[dict]) -> list[LabeledItem]:
    """Replay frozen `recorded_*` fields through the REAL deterministic engine → `LabeledItem`s.

    The one impure bridge (and the SINGLE replay code path the script and the test both call —
    no duplication). For each **claim** row it feeds `recorded_judgment` + the recorded
    `evidence` through `proov.llm._normalise_judgment` (so the offline run exercises the actual
    grounding + label-needs-evidence anti-guess guards) to get the predicted `status`; for each
    **citation** row it feeds `recorded_fetch` through the updated
    `proov.citations._classify_retrievability` + `_flag_for` to get the predicted `flag`. NO
    network, NO API spend — fully deterministic. The engine seams are imported lazily so
    importing this module stays stdlib-pure (the scoring math above never touches them).
    """
    from .citations import _classify_retrievability, _flag_for
    from .llm import _normalise_judgment
    from .types import Evidence

    items: list[LabeledItem] = []
    for row in rows:
        kind = row.get("kind")
        thin = bool(row.get("thin_evidence", False))
        if kind == "claim":
            evidence = [
                Evidence(
                    source=e.get("source", ""),
                    title=e.get("title", ""),
                    snippet=e.get("snippet", ""),
                    score=e.get("score"),
                )
                for e in row.get("evidence", [])
                if isinstance(e, dict)
            ]
            judgment = _normalise_judgment(row.get("recorded_judgment") or {}, evidence)
            items.append(
                LabeledItem(
                    id=row["id"],
                    predicted=judgment.status,
                    gold=row["gold_status"],
                    kind="claim",
                    thin_evidence=thin,
                )
            )
        elif kind == "citation":
            fetch = row.get("recorded_fetch") or {}
            cls = _classify_retrievability(
                fetch.get("http_status"), transport_error="transport_error" in fetch
            )
            _retrievable, _supports, flag = _flag_for(cls, row.get("recorded_support_status"))
            items.append(
                LabeledItem(
                    id=row["id"],
                    predicted=flag,
                    gold=row["gold_flag"],
                    kind="citation",
                    thin_evidence=thin,
                )
            )
        else:
            raise ValueError(f"calibration row {row.get('id')!r} has unknown kind {kind!r}")
    return items
