from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jobradar.pipeline_benchmark import FrozenDataset
from jobradar.schemas import CVProfile
from jobradar.version_comparison import (
    alternating_order,
    build_version_comparison,
    write_comparison_reports,
)
from jobradar.version_matrix import (
    build_matrix_report,
    render_matrix_markdown,
    rotated_order,
)
from scripts.pipeline_version_worker import _load_batches

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "tests" / "fixtures" / "pipeline_benchmark.json"


def _version_run(mode: str, elapsed: float, first: float, keys: list[str]) -> dict:
    return {
        "version_mode": mode,
        "dataset_hash": "dataset-hash",
        "profile_hash": "profile-hash",
        "provider": "test",
        "model": "test-model",
        "total_elapsed": elapsed,
        "time_to_first_job": first,
        "result_keys": keys,
        "llm_calls": 3,
        "tokens_in": 100,
        "tokens_out": 20,
        "assessment_workers": 1 if mode == "baseline" else 5,
        "timing_mode": "recorded",
        "speed_factor": 1.0,
        "replay_contract": {
            "query_event_count": 2,
            "empty_query_event_count": 1,
            "nonempty_batch_count": 1,
            "candidate_count": 2,
            "producer_finished_offset_seconds": 1.0,
            "last_candidate_ready_offset_seconds": 0.8,
            "producer_tail_seconds": 0.2,
        },
    }


def test_version_comparison_aggregates_paired_runs(tmp_path: Path) -> None:
    baseline = [
        _version_run("baseline", 10.0, 8.0, ["a", "b"]),
        _version_run("baseline", 12.0, 9.0, ["a", "b"]),
    ]
    candidate = [
        _version_run("candidate", 7.0, 3.0, ["a", "b"]),
        _version_run("candidate", 8.0, 4.0, ["a", "c"]),
    ]

    report = build_version_comparison(
        baseline,
        candidate,
        baseline_ref="old",
        candidate_ref="new",
    )

    assert alternating_order(0) == ("baseline", "candidate")
    assert alternating_order(1) == ("candidate", "baseline")
    assert report["improvement_percent"]["total_mean"] > 0
    assert report["result_comparison"]["exact_pairs"] == 1
    assert report["result_comparison"]["jaccard_min"] == 1 / 3

    controlled = {
        "dataset_id": "fixture",
        "dataset_hash": "dataset-hash",
        "result_equivalent": True,
        "serial": {"total_mean": 1.0, "total_p50": 1.0, "first_result_mean": 0.8, "overlap_mean": 0},
        "streaming": {
            "total_mean": 0.7,
            "total_p50": 0.7,
            "first_result_mean": 0.2,
            "overlap_mean": 0.5,
        },
    }
    json_path, markdown_path = write_comparison_reports(
        tmp_path,
        controlled=controlled,
        real=report,
        baseline_runs=baseline,
        candidate_runs=candidate,
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["real"]["baseline_ref"] == "old"
    assert "Historical real-LLM comparison" in markdown_path.read_text(encoding="utf-8")


def test_version_worker_preserves_full_dataset_hash_and_producer_tail() -> None:
    frozen = FrozenDataset.load(DATASET)
    _, batches, dataset_hash, producer_finished, contract = _load_batches(DATASET)

    assert dataset_hash == frozen.dataset_hash
    assert contract["nonempty_batch_count"] == len(batches)
    assert contract["candidate_count"] == 5
    assert contract["producer_finished_offset_seconds"] == producer_finished == 0.05
    assert contract["producer_tail_seconds"] == pytest.approx(0.01)


def test_one_command_controller_runs_offline_controlled_comparison(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        CVProfile(
            summary="Python AI engineer",
            skills=["Python", "LLM"],
            preferred_roles=["AI Engineer"],
            seniority="junior",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    output_dir = tmp_path / "comparison"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compare_pipeline_versions.py"),
            "--profile",
            str(profile_path),
            "--dataset",
            str(DATASET),
            "--output-dir",
            str(output_dir),
            "--controlled-runs",
            "1",
            "--controlled-warmups",
            "0",
            "--assessment-delay",
            "0",
            "--assessment-workers",
            "5",
            "--timing",
            "instant",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "JSON report:" in completed.stdout
    payload = json.loads((output_dir / "version_comparison.json").read_text(encoding="utf-8"))
    assert payload["controlled"]["result_equivalent"] is True
    assert payload["real"] is None
    assert "Not run" in (output_dir / "version_comparison.md").read_text(encoding="utf-8")


def test_assessment_worker_controller_runs_isolated_offline_smoke(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        CVProfile(
            summary="Python AI engineer",
            skills=["Python", "LLM"],
            preferred_roles=["AI Engineer"],
            seniority="junior",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    dataset_path = tmp_path / "empty-dataset.json"
    dataset_path.write_text(
        json.dumps(
            {
                "dataset_id": "empty-worker-comparison",
                "batches": [{"batch_id": "empty", "ready_offset_seconds": 0, "jobs": []}],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "worker-comparison"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compare_assessment_workers.py"),
            "--dataset",
            str(dataset_path),
            "--profile",
            str(profile_path),
            "--output-dir",
            str(output_dir),
            "--real-llm",
            "--provider",
            "fake",
            "--model",
            "fake-model",
            "--runs",
            "1",
            "--candidate-workers",
            "5",
            "--timing",
            "instant",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "JSON report:" in completed.stdout
    payload = json.loads((output_dir / "assessment_worker_comparison.json").read_text(encoding="utf-8"))
    assert payload["comparison"]["baseline"]["runs"] == 1
    assert payload["candidate_worker_count"] == 5
    assert payload["comparison"]["result_comparison"]["jaccard_mean"] == 1
    assert (output_dir / "assessment_worker_comparison.md").exists()


def test_matrix_report_separates_model_and_concurrency_effects() -> None:
    def matrix_run(model: str, elapsed: float, workers: int, keys: list[str]) -> dict:
        return {
            **_version_run("baseline" if workers == 1 else "candidate", elapsed, elapsed / 2, keys),
            "model": model,
            "pipeline_stats": {
                "llm_assessed": 12,
                "evaluation_tasks": 12,
                "evaluation_peak_inflight": workers,
                "evaluation_failed": 0,
            },
        }

    runs = {
        "reference": {
            "baseline": [matrix_run("model-a", 12, 1, ["a", "b"])],
            "current-3w": [matrix_run("model-a", 6, 3, ["a", "b"])],
            "current-5w": [matrix_run("model-a", 5, 5, ["a", "c"])],
        },
        "replacement": {
            "baseline": [matrix_run("model-b", 9, 1, ["a", "b"])],
            "current-3w": [matrix_run("model-b", 4, 3, ["a", "b"])],
        },
    }

    report = build_matrix_report(
        runs,
        model_names={"reference": "model-a", "replacement": "model-b"},
        baseline_ref="old",
        candidate_ref="new",
    )

    assert rotated_order(("a", "b", "c"), 1) == ("b", "c", "a")
    assert report["within_model_contrasts"]["reference"]["current-3w"]["change_percent"]["total_mean"] == 50
    assert report["cross_model_contrasts"]["baseline"]["change_percent"]["total_mean"] == 25
    assert report["worker_count_contrasts"]["reference"]["change_percent"]["total_mean"] > 0
    assert report["model_concurrency_interaction"]["difference_percentage_points"] > 0
    assert "Model × concurrency interaction" in render_matrix_markdown(report)
    assert "Current 3-worker versus 5-worker" in render_matrix_markdown(report)
