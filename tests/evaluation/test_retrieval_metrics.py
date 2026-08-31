from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from evaluation.retrieval_metrics import (
    compare,
    evaluate_rankings,
    main,
    summarize,
)


# --- Interval-aware metric semantics ---------------------------------------------


def test_metrics_use_interval_containment_and_top_k() -> None:
    judgments = {
        "q1": [("doc-a", 1, 3), ("doc-b", 2, 2)],
        "q2": [],
    }
    rankings = {
        # doc-x is irrelevant, doc-a page 2 is contained in [1,3], doc-b page 2 matches.
        "q1": [("doc-x", 1), ("doc-a", 2), ("doc-b", 2)],
        "q2": [],
    }
    metrics = evaluate_rankings(judgments, rankings, k=2)
    q1 = next(item for item in metrics if item.query_id == "q1")
    assert q1.precision_at_k == 0.5
    assert q1.recall_at_k == 0.5
    assert q1.reciprocal_rank == 0.5
    q2 = next(item for item in metrics if item.query_id == "q2")
    assert q2.precision_at_k == 0.0
    assert q2.recall_at_k == 0.0
    assert q2.reciprocal_rank == 0.0

    summary = summarize(metrics)
    assert summary == {"queryCount": 2, "precisionAtK": 0.25, "recallAtK": 0.25, "mrr": 0.25}


def test_two_returned_pages_from_one_judged_interval_count_once_for_recall() -> None:
    """Precision counts every contained page; recall counts each judged interval at most once."""
    judgments = {"q": [("doc-a", 1, 3)]}
    rankings = {"q": [("doc-a", 1), ("doc-a", 2), ("doc-a", 3)]}
    metrics = evaluate_rankings(judgments, rankings, k=5)
    q = metrics[0]
    # All three retrieved pages match the interval => precision 3/5.
    assert q.precision_at_k == pytest.approx(0.6)
    # But the single judged interval only contributes 1/1 to recall.
    assert q.recall_at_k == 1.0
    assert q.reciprocal_rank == 1.0


def test_mrr_uses_first_containment_hit_even_after_miss() -> None:
    judgments = {"q": [("doc-a", 5, 7)]}
    rankings = {"q": [("doc-a", 4), ("doc-a", 6), ("doc-a", 8)]}
    q = evaluate_rankings(judgments, rankings, k=5)[0]
    assert q.reciprocal_rank == 0.5


def test_precision_denominator_is_configured_k_not_retrieved_count() -> None:
    judgments = {"q": [("doc-a", 1, 1)]}
    rankings = {"q": [("doc-a", 1)]}
    q = evaluate_rankings(judgments, rankings, k=5)[0]
    assert q.precision_at_k == pytest.approx(0.2)
    assert q.recall_at_k == 1.0


# --- Exact-set enforcement --------------------------------------------------------


def test_evaluate_rankings_rejects_query_set_mismatch() -> None:
    with pytest.raises(ValueError, match="same queryIds"):
        evaluate_rankings({"q1": []}, {"q2": []}, k=1)


def test_evaluate_rankings_rejects_extra_ranking_query() -> None:
    with pytest.raises(ValueError, match="same queryIds"):
        evaluate_rankings({"q1": []}, {"q1": [], "q2": []}, k=1)


def test_evaluate_rankings_rejects_boolean_k() -> None:
    with pytest.raises(ValueError, match="k must be a positive integer"):
        evaluate_rankings({"q": []}, {"q": []}, k=True)  # type: ignore[arg-type]


def test_evaluate_rankings_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError, match="k must be a positive integer"):
        evaluate_rankings({"q": []}, {"q": []}, k=0)


# --- Regression semantics ---------------------------------------------------------


def test_compare_reports_aggregate_and_per_query_regression() -> None:
    judgments = {
        "improved": [("doc-a", 1, 1)],
        "regressed": [("doc-b", 1, 1)],
    }
    baseline = {
        "improved": [("doc-x", 1), ("doc-a", 1)],
        "regressed": [("doc-b", 1)],
    }
    candidate = {
        "improved": [("doc-a", 1)],
        "regressed": [("doc-x", 1), ("doc-b", 1)],
    }
    report = compare(judgments, baseline, candidate, k=2)
    assert report["delta"]["precisionAtK"] == 0.0
    assert report["delta"]["recallAtK"] == 0.0
    assert report["delta"]["mrr"] == 0.0
    assert [item["queryId"] for item in report["regressions"]] == ["regressed"]


# --- Ground-truth and ranking validation ------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _minimal_ground_truth() -> list[dict[str, object]]:
    return [
        {
            "queryId": "q1",
            "expectedContext": [
                {"documentItemId": "doc-a", "pageStart": 1, "pageEnd": 1},
            ],
        }
    ]


def _minimal_ranking() -> list[dict[str, object]]:
    return [
        {
            "queryId": "q1",
            "retrievedContext": [
                {"documentItemId": "doc-a", "pageNumber": 1},
            ],
        }
    ]


def _cli_args(
    ground: Path,
    baseline: Path,
    candidate: Path,
    *,
    k: int = 5,
) -> list[str]:
    manifest = ground.parent / "experiment-manifest.json"
    manifest.write_text(json.dumps({
        "k": k,
        "datasetComponents": {
            "groundTruthHash": hashlib.sha256(ground.read_bytes()).hexdigest(),
        },
        "baselineRankingHash": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "candidateRankingHash": hashlib.sha256(candidate.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return [
        "--ground-truth", str(ground),
        "--baseline", str(baseline),
        "--candidate", str(candidate),
        "--manifest", str(manifest),
        "--k", str(k),
    ]


def test_cli_default_regression_budget_blocks_any_regression(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(ground, [
        {"queryId": "q", "expectedContext": [
            {"documentItemId": "doc-a", "pageStart": 1, "pageEnd": 1},
        ]},
    ])
    _write_jsonl(baseline, [
        {"queryId": "q", "retrievedContext": [
            {"documentItemId": "doc-a", "pageNumber": 1},
        ]},
    ])
    _write_jsonl(candidate, [
        {"queryId": "q", "retrievedContext": [
            {"documentItemId": "doc-x", "pageNumber": 1},
            {"documentItemId": "doc-a", "pageNumber": 1},
        ]},
    ])
    exit_code = main(_cli_args(ground, baseline, candidate, k=2))
    # MRR drops from 1.0 to 0.5 => per-query regression => default budget 0 fails.
    assert exit_code == 1


def test_cli_regression_budget_allows_absorbed_regressions(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    _write_jsonl(baseline, _minimal_ranking())
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main(_cli_args(ground, baseline, candidate, k=2))
    assert exit_code == 0


def test_cli_rejects_ranking_changed_after_manifest(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    _write_jsonl(baseline, _minimal_ranking())
    _write_jsonl(candidate, _minimal_ranking())
    args = _cli_args(ground, baseline, candidate)
    _write_jsonl(candidate, [{"queryId": "q1", "retrievedContext": []}])

    exit_code = main(args)

    assert exit_code == 2


def test_cli_rejects_non_finite_threshold(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    _write_jsonl(baseline, _minimal_ranking())
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main([
        *_cli_args(ground, baseline, candidate),
        "--min-precision-delta", "nan",
    ])
    assert exit_code == 2


def test_cli_rejects_negative_regression_budget(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    _write_jsonl(baseline, _minimal_ranking())
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main([
        *_cli_args(ground, baseline, candidate),
        "--regression-budget", "-1",
    ])
    assert exit_code == 2


def test_ground_truth_rejects_overlapping_intervals(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    _write_jsonl(ground, [
        {"queryId": "q", "expectedContext": [
            {"documentItemId": "doc-a", "pageStart": 1, "pageEnd": 3},
            {"documentItemId": "doc-a", "pageStart": 3, "pageEnd": 5},
        ]},
    ])
    baseline = tmp_path / "baseline.jsonl"
    _write_jsonl(baseline, _minimal_ranking())
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main(_cli_args(ground, baseline, candidate))
    assert exit_code == 2


def test_ground_truth_rejects_duplicate_query_id(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    _write_jsonl(ground, [
        {"queryId": "q", "expectedContext": [
            {"documentItemId": "doc-a", "pageStart": 1, "pageEnd": 1},
        ]},
        {"queryId": "q", "expectedContext": [
            {"documentItemId": "doc-b", "pageStart": 1, "pageEnd": 1},
        ]},
    ])
    baseline = tmp_path / "baseline.jsonl"
    _write_jsonl(baseline, _minimal_ranking())
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main(_cli_args(ground, baseline, candidate))
    assert exit_code == 2


def test_ranking_rejects_duplicate_returned_identity(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    baseline = tmp_path / "baseline.jsonl"
    _write_jsonl(baseline, _minimal_ranking())
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(candidate, [
        {"queryId": "q1", "retrievedContext": [
            {"documentItemId": "doc-a", "pageNumber": 1},
            {"documentItemId": "doc-a", "pageNumber": 1},
        ]},
    ])
    exit_code = main(_cli_args(ground, baseline, candidate))
    assert exit_code == 2


def test_ranking_rejects_non_positive_page_number(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    baseline = tmp_path / "baseline.jsonl"
    _write_jsonl(baseline, [
        {"queryId": "q1", "retrievedContext": [
            {"documentItemId": "doc-a", "pageNumber": 0},
        ]},
    ])
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main(_cli_args(ground, baseline, candidate))
    assert exit_code == 2


def test_ranking_rejects_boolean_page_number(tmp_path: Path) -> None:
    ground = tmp_path / "gt.jsonl"
    _write_jsonl(ground, _minimal_ground_truth())
    baseline = tmp_path / "baseline.jsonl"
    _write_jsonl(baseline, [
        {"queryId": "q1", "retrievedContext": [
            {"documentItemId": "doc-a", "pageNumber": True},
        ]},
    ])
    candidate = tmp_path / "candidate.jsonl"
    _write_jsonl(candidate, _minimal_ranking())
    exit_code = main(_cli_args(ground, baseline, candidate))
    assert exit_code == 2
