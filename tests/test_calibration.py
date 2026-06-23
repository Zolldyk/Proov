"""Tests for the calibration scorer + the offline precision gate (`proov/calibration.py`).

OFFLINE ONLY, $0, deterministic (NFR1): the scorer math runs over hand-built confusion
matrices, and the dataset gate replays the committed `calibration/calibration_set.json` frozen
`recorded_*` fields through the REAL deterministic engine (`proov.calibration.replay_dataset`
-> `_normalise_judgment` / `_classify_retrievability` / `_flag_for`) — NO Gemini, NO Tavily, NO
sockets, NO wall-clock. The replay is the SAME code path `scripts/calibrate.py` runs (no
duplication). The autouse `tests/conftest.py` cache-disable already isolates this.
"""

from __future__ import annotations

from proov.calibration import (
    FLAG_LABELS,
    CalibrationReport,
    ClassMetrics,
    LabeledItem,
    load_dataset,
    meets_bar,
    replay_dataset,
    score,
)

# --------------------------------------------------------------------------- scorer math


def _claim(predicted: str, gold: str, thin: bool = False, _id: str = "x") -> LabeledItem:
    return LabeledItem(id=_id, predicted=predicted, gold=gold, kind="claim", thin_evidence=thin)


def test_precision_recall_match_hand_computed_confusion_matrix():
    items = [
        _claim("unsupported", "unsupported"),  # tp unsupported
        _claim("unsupported", "unsupported"),  # tp unsupported
        _claim("unsupported", "supported"),    # fp unsupported / fn supported
        _claim("supported", "supported"),      # tp supported
        _claim("supported", "unsupported"),    # fp supported / fn unsupported
    ]
    report = score(items)

    unsup = report.per_class["unsupported"]
    assert (unsup.tp, unsup.fp, unsup.fn) == (2, 1, 1)
    assert unsup.precision == 2 / 3  # 2 tp / (2 tp + 1 fp)
    assert unsup.recall == 2 / 3     # 2 tp / (2 tp + 1 fn)

    sup = report.per_class["supported"]
    assert (sup.tp, sup.fp, sup.fn) == (1, 1, 1)
    assert sup.precision == 0.5
    assert sup.recall == 0.5


def test_zero_prediction_class_has_none_precision_not_zero():
    # "fabricated" is a gold label but is NEVER predicted -> precision UNDEFINED (None), excluded
    # from the gate; recall is defined (0 tp / 1 gold = 0.0). The inverse holds for "ok".
    items = [LabeledItem(id="1", predicted="ok", gold="fabricated", kind="citation")]
    report = score(items)

    fab = report.per_class["fabricated"]
    assert fab.precision is None          # zero predictions -> undefined, NOT 0.0
    assert fab.recall == 0.0              # gold positive exists, none recalled

    ok = report.per_class["ok"]
    assert ok.precision == 0.0            # one (wrong) prediction -> defined, and 0.0
    assert ok.recall is None             # no gold positives -> recall undefined


def test_meets_bar_is_exact_at_the_080_boundary():
    # 4/5 = 0.80 PASSES (>= threshold); 3/4 = 0.75 FAILS.
    pass_items = [_claim("unsupported", "unsupported") for _ in range(4)]
    pass_items.append(_claim("unsupported", "supported"))  # 1 fp -> 4/5 = 0.80
    pass_report = score(pass_items)
    assert pass_report.per_class["unsupported"].precision == 0.80
    assert meets_bar(pass_report, 0.80) is True

    fail_items = [_claim("unsupported", "unsupported") for _ in range(3)]
    fail_items.append(_claim("unsupported", "supported"))  # 1 fp -> 3/4 = 0.75
    fail_report = score(fail_items)
    assert fail_report.per_class["unsupported"].precision == 0.75
    assert meets_bar(fail_report, 0.80) is False


def test_meets_bar_ignores_a_zero_prediction_flag_class():
    # unsupported is exactly at the bar; fabricated is never predicted (None) -> does NOT fail.
    items = [_claim("unsupported", "unsupported") for _ in range(4)]
    items.append(_claim("unsupported", "supported"))  # 4/5 = 0.80
    report = score(items)
    assert report.per_class["unsupported"].precision == 0.80
    assert "fabricated" not in report.per_class  # never seen
    assert meets_bar(report, 0.80) is True


def test_pooled_flag_precision_pools_both_flag_classes():
    items = [
        _claim("unsupported", "unsupported"),                                  # unsup tp
        _claim("unsupported", "unsupported"),                                  # unsup tp
        LabeledItem(id="f1", predicted="fabricated", gold="fabricated", kind="citation"),  # fab tp
        LabeledItem(id="f2", predicted="fabricated", gold="ok", kind="citation"),          # fab fp
    ]
    report = score(items)
    assert report.per_class["unsupported"].precision == 1.0
    assert report.per_class["fabricated"].precision == 0.5
    # pooled = (2 unsup tp + 1 fab tp) / (2 unsup pred + 2 fab pred) = 3/4 = 0.75
    assert report.pooled_flag_precision == 0.75
    # A flag class below the bar (fabricated 0.5) fails the gate even though pooled is mid.
    assert meets_bar(report, 0.80) is False


def test_pooled_is_none_when_nothing_flagged_and_bar_is_vacuously_met():
    items = [_claim("supported", "supported"), _claim("unverifiable", "unverifiable", thin=True)]
    report = score(items)
    assert report.pooled_flag_precision is None
    assert meets_bar(report, 0.80) is True  # flagged nothing -> no precision failure


def test_thin_evidence_counts_track_unverifiable_predictions():
    items = [
        _claim("unverifiable", "unverifiable", thin=True),
        _claim("unverifiable", "unverifiable", thin=True),
        _claim("supported", "supported"),  # not thin
    ]
    report = score(items)
    assert report.thin_total == 2
    assert report.thin_unverifiable == 2


def test_report_value_types_are_frozen_dataclasses():
    items = [_claim("supported", "supported")]
    report = score(items)
    assert isinstance(report, CalibrationReport)
    assert isinstance(report.per_class["supported"], ClassMetrics)
    assert report.flag_labels == FLAG_LABELS


# --------------------------------------------------------------------------- the dataset gate
# Replay the COMMITTED frozen set through the real deterministic pipeline and assert the >=80%
# precision bar, the 100% thin -> unverifiable rate, and a meaningful (non-rigged) gate.


def _dataset_report() -> CalibrationReport:
    items = replay_dataset(load_dataset())
    return score(items)


def test_committed_dataset_clears_the_080_precision_bar():
    report = _dataset_report()
    assert report.pooled_flag_precision is not None
    assert report.pooled_flag_precision >= 0.80
    assert report.per_class["unsupported"].precision >= 0.80
    assert report.per_class["fabricated"].precision >= 0.80
    assert meets_bar(report, 0.80) is True


def test_committed_dataset_resolves_every_thin_row_to_unverifiable():
    items = replay_dataset(load_dataset())
    thin = [it for it in items if it.thin_evidence]
    assert thin, "dataset must exercise the thin-evidence -> unverifiable guard"
    assert all(it.predicted == "unverifiable" for it in thin)  # 100%


def test_committed_dataset_is_a_meaningful_gate_not_a_tautology():
    # Guards against a future rigged/all-correct set silently passing: at least one flag class
    # must contain a real false positive (precision < 1.0), so the bar is a genuine threshold.
    report = _dataset_report()
    flag_precisions = [
        report.per_class[label].precision
        for label in FLAG_LABELS
        if label in report.per_class and report.per_class[label].precision is not None
    ]
    assert any(p < 1.0 for p in flag_precisions)


def test_committed_dataset_has_both_row_kinds_and_is_about_fifty_rows():
    rows = load_dataset()
    kinds = {row.get("kind") for row in rows}
    assert kinds == {"claim", "citation"}
    assert 40 <= len(rows) <= 60
