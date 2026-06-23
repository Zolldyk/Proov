"""Calibration runner — score Proov's verdict precision against the hand-labeled set (Story 3.1).

TEST TOOLING / OPS, not product. Two modes:

  * **Replay (default, offline, deterministic — the suite's gate).** `python scripts/calibrate.py`
    loads `calibration/calibration_set.json` and replays each row's frozen `recorded_*` fields
    through the REAL deterministic engine code (`proov.llm._normalise_judgment`,
    `proov.citations._classify_retrievability`/`_flag_for`) — NO network, NO API spend, fully
    reproducible. It prints a `CalibrationReport` (per-class precision/recall, pooled flag
    precision, thin-evidence -> unverifiable rate) and an explicit PASS/FAIL against the 0.80
    bar, exiting non-zero on FAIL. This is the same code path `tests/test_calibration.py` gates
    on (via `proov.calibration.replay_dataset`).

  * **`--live` (real providers — the operator's empirical run; spends quota, requires keys).**
    `python scripts/calibrate.py --live` calls the real engine (`get_llm_provider()` +
    `retrieve_evidence` + a real fetch) over the dataset's claims/urls to refresh the recorded
    inputs, writes the refreshed snapshot back to the JSON, then re-scores. It refreshes ONLY:
    claim rows -> `evidence` + `recorded_judgment`; citation rows -> `recorded_fetch`. It does
    NOT regenerate citation `recorded_support_status` (no synthetic-claim/output is stored per
    citation row to re-judge against) nor any `gold_*` hand label. **A refresh therefore
    overwrites `recorded_judgment` wholesale — which can erase the deliberately seeded
    honest-error rows (e.g. `claim-unsup-fp-01`) that make the gate a meaningful threshold, and
    leaves `recorded_support_status` as hand-maintained carry-over.** After any `--live` run the
    operator MUST re-review the dataset's `expected` header and the seeded false-positive rows
    before committing the snapshot. It is NEVER imported by the suite or by `proov.provider` — no
    engine/SDK coupling is added by this script existing.

Run from the repo root:
    python scripts/calibrate.py            # offline replay + gate (the committed snapshot)
    python scripts/calibrate.py --live     # real Gemini/Tavily; refresh + re-score (spend, keys)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

# Allow `python scripts/calibrate.py` from the repo root to import the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proov.calibration import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    CalibrationReport,
    load_dataset,
    meets_bar,
    replay_dataset,
    score,
)

_THRESHOLD = 0.80
log = logging.getLogger("calibrate")


def _fmt(value: float | None) -> str:
    """Render a precision/recall: a 3-dp percentage, or `n/a` when undefined (`None`)."""
    return "  n/a" if value is None else f"{value:6.3f}"


def _format_report(report: CalibrationReport) -> str:
    """Render a `CalibrationReport` as a human-readable per-class table + the gate summary."""
    lines = [
        "Calibration report (precision over recall — NFR4; only flagged classes gate the bar)",
        f"  {'label':14} {'pred':>4} {'tp':>3} {'fp':>3} {'fn':>3} {'precision':>9} {'recall':>9}",
    ]
    for label in sorted(report.per_class):
        m = report.per_class[label]
        flag = " *" if label in report.flag_labels else "  "
        lines.append(
            f"{flag}{m.label:14} {m.predicted_total:>4} {m.tp:>3} {m.fp:>3} {m.fn:>3} "
            f"{_fmt(m.precision):>9} {_fmt(m.recall):>9}"
        )
    lines.append("  (* = verdict-flipping flag class the >=80% bar gates)")
    lines.append(f"  pooled flag precision : {_fmt(report.pooled_flag_precision)}")
    thin_rate = (
        report.thin_unverifiable / report.thin_total if report.thin_total else 1.0
    )
    lines.append(
        f"  thin -> unverifiable   : {report.thin_unverifiable}/{report.thin_total} "
        f"({thin_rate:.0%})"
    )
    return "\n".join(lines)


def _gate(report: CalibrationReport) -> int:
    """Print the PASS/FAIL gate verdict against the 0.80 bar; return a process exit code."""
    passed = meets_bar(report, _THRESHOLD)
    thin_ok = report.thin_total == 0 or report.thin_unverifiable == report.thin_total
    print(_format_report(report))
    if passed and thin_ok:
        print(f"\nPASS — flag-class precision >= {_THRESHOLD:.2f} and thin-evidence -> unverifiable holds.")
        return 0
    if not passed:
        print(f"\nFAIL — flag-class precision is below the {_THRESHOLD:.2f} bar.")
    if not thin_ok:
        print(
            f"\nFAIL — {report.thin_total - report.thin_unverifiable} thin-evidence row(s) did "
            "NOT resolve to unverifiable."
        )
    return 1


def _run_replay() -> int:
    """Offline replay (default): load the frozen set, replay deterministically, score, gate."""
    rows = load_dataset()
    log.info("replaying %d rows from %s (offline, deterministic)", len(rows), DEFAULT_DATASET_PATH)
    report = score(replay_dataset(rows))
    return _gate(report)


async def _refresh_live(rows: list[dict]) -> None:
    """Regenerate every row's `recorded_*` from the REAL engine (spends quota; requires keys)."""
    import httpx

    from proov.citations import _DEFAULT_CITATION_TIMEOUT, _resolve_user_agent
    from proov.llm import LLMError, get_llm_provider, judge_claim
    from proov.search import retrieve_evidence
    from proov.types import Claim

    # Resolve the provider once up front so a missing key fails fast with a clear message.
    try:
        provider = get_llm_provider()
    except LLMError as exc:
        raise SystemExit(f"--live needs a configured LLM provider: {exc}") from exc

    headers = {"User-Agent": _resolve_user_agent()}
    for row in rows:
        kind = row.get("kind")
        if kind == "claim":
            claim_text = row["claim"]
            evidence = await retrieve_evidence(claim_text, "deep")
            judgment = await judge_claim(
                Claim(id=row["id"], text=claim_text), evidence, "deep", provider=provider
            )
            row["evidence"] = [
                {"source": e.source, "title": e.title, "snippet": e.snippet, "score": e.score}
                for e in evidence
            ]
            row["recorded_judgment"] = {
                "status": judgment.status,
                "confidence": judgment.confidence,
                "evidence": [
                    {"source": es.source, "quote": es.quote, "stance": es.stance}
                    for es in judgment.evidence
                ],
            }
            log.info("claim %s -> %s (%d evidence)", row["id"], judgment.status, len(evidence))
        elif kind == "citation":
            url = row["url"]
            try:
                async with httpx.AsyncClient(
                    timeout=_DEFAULT_CITATION_TIMEOUT, follow_redirects=True, headers=headers
                ) as client:
                    response = await client.get(url)
                row["recorded_fetch"] = {"http_status": response.status_code}
                log.info("citation %s -> http %s", row["id"], response.status_code)
            except httpx.HTTPError as exc:
                row["recorded_fetch"] = {"transport_error": type(exc).__name__}
                log.info("citation %s -> transport_error %s", row["id"], type(exc).__name__)
        else:
            log.warning("skipping row %s with unknown kind %r", row.get("id"), kind)


def _run_live() -> int:
    """`--live`: refresh the frozen snapshot from the real engine, write it back, then re-score."""
    path = DEFAULT_DATASET_PATH
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    rows = document["rows"] if isinstance(document, dict) else document

    log.info("LIVE calibration over %d rows — this calls real providers and spends quota", len(rows))
    log.warning(
        "--live overwrites recorded_judgment/recorded_fetch in place; it does NOT regenerate "
        "citation recorded_support_status or any gold_* label. Re-review the dataset 'expected' "
        "header and the seeded false-positive rows (e.g. claim-unsup-fp-01) before committing — "
        "a refresh can erase the seeded errors that make the >=80%% gate a meaningful threshold."
    )
    asyncio.run(_refresh_live(rows))

    # Persist the refreshed snapshot (preserving the header/`expected` note on the object form).
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    log.info("refreshed snapshot written to %s", path)

    report = score(replay_dataset(rows))
    return _gate(report)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    live = "--live" in sys.argv[1:]
    return _run_live() if live else _run_replay()


if __name__ == "__main__":
    sys.exit(main())
