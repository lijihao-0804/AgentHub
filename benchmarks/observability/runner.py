"""Validate the M6 controlled failure-scenario manifest.

The manifest describes cases that must be produced through AgentRunService failure injection;
it never contains persisted Run rows, credentials, prompts, or customer data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.observability.schema import dataset_summary, load_dataset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "observability" / "dataset.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    print(json.dumps(dataset_summary(dataset), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
