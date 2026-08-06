"""Export a cached CVProfile without printing its contents."""
from __future__ import annotations

import argparse
from pathlib import Path

from jobradar import cache


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a cached CVProfile for pipeline benchmarking")
    parser.add_argument("--output", required=True)
    parser.add_argument("--cv-hash", default="", help="Defaults to the most recently cached CVProfile")
    args = parser.parse_args()

    profile = cache.get_cv_profile(args.cv_hash) if args.cv_hash else cache.get_latest_cv_profile()
    if profile is None:
        raise SystemExit("No cached CVProfile was found")
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(f"CVProfile exported to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
