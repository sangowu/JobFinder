"""Score two completed blind human-review files against model decisions."""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobradar.model_quality_audit import score_blind_reviews


def main() -> int:
    parser = argparse.ArgumentParser(description="Score two completed blinded quality reviews")
    parser.add_argument("--report", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    parser.add_argument("--adjudication", help="Optional completed adjudication CSV")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    with Path(args.reviewer_a).open(encoding="utf-8-sig", newline="") as handle:
        reviewer_a = list(csv.DictReader(handle))
    with Path(args.reviewer_b).open(encoding="utf-8-sig", newline="") as handle:
        reviewer_b = list(csv.DictReader(handle))
    adjudication = []
    if args.adjudication:
        with Path(args.adjudication).open(encoding="utf-8-sig", newline="") as handle:
            adjudication = list(csv.DictReader(handle))
    score = score_blind_reviews(report, manifest, reviewer_a, reviewer_b, adjudication)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Score: {output}")
    print(f"Status: {score['status']}")
    print(f"Reviewer agreement: {score['reviewer_agreement_rate']:.1%}")
    if score["status"] == "needs_adjudication":
        adjudication_path = output.with_name("adjudication_required.csv")
        labels_a = {row["blind_id"]: row["human_decision"] for row in reviewer_a}
        labels_b = {row["blind_id"]: row["human_decision"] for row in reviewer_b}
        with adjudication_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("blind_id", "reviewer_a", "reviewer_b", "human_decision", "reason"),
            )
            writer.writeheader()
            for blind_id in score["unresolved_jobs"]:
                writer.writerow(
                    {
                        "blind_id": blind_id,
                        "reviewer_a": labels_a[blind_id],
                        "reviewer_b": labels_b[blind_id],
                        "human_decision": "",
                        "reason": "",
                    }
                )
        print(f"Adjudication required: {adjudication_path}")
    return 0 if score["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
