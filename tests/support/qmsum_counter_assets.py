"""Rebuild the four 2026-10-27 summary inputs from the pinned public QMSum release."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from benchmarks.evaluation.memory_qmsum_live import (
    freeze_summary_inputs,
    prepare_reviewed_summaries,
)
from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSUM_COMMIT,
    frozen_meetings,
    load_frozen_qmsum_files,
    plan_offline,
)
from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import ApproxTokenEstimator

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY_EVIDENCE = REPO_ROOT / "docs" / "reviews" / "evidence" / "context-policy-m4-20261027"
ORIGINAL_STUDY = REPO_ROOT / ".scratch" / "context-policy-m4-20261027"
QMSUM_RELEASE = f"https://raw.githubusercontent.com/Yale-LILY/QMSum/{QMSUM_COMMIT}"
PUBLIC_SOURCE_FILES = (
    Path("data/ALL/jsonl/train.jsonl"),
    *(Path(f"data/ALL/train/{meeting.meeting_id}.json") for meeting in frozen_meetings()),
)


def rebuild_frozen_inputs(source_root: Path, destination: Path) -> None:
    source = load_frozen_qmsum_files(source_root)
    annotations = json.loads((STUDY_EVIDENCE / "qmsum-protected-turns-v1.json").read_bytes())
    policy = create_trial_policy()
    plan = plan_offline(source, admission=policy, token_counter=ApproxTokenEstimator())
    preparations = prepare_reviewed_summaries(source, plan, annotations, policy=policy)
    freeze_summary_inputs(preparations, destination)


def download_source_files(destination: Path) -> None:
    """Fetch only the seven files verified by the study loader; never fetch model outputs."""
    with httpx.Client(timeout=30) as client:
        for relative_path in PUBLIC_SOURCE_FILES:
            path = destination / relative_path
            if path.is_file():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with client.stream("GET", f"{QMSUM_RELEASE}/{relative_path.as_posix()}") as response:
                response.raise_for_status()
                # The source loader verifies every file (including cached ones) before use.
                partial = path.with_name(f"{path.name}.{uuid4().hex}.part")
                try:
                    with partial.open("xb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                    os.replace(partial, path)
                finally:
                    partial.unlink(missing_ok=True)


def resolve_frozen_inputs() -> Path:
    configured = os.environ.get("AGENTHUB_DEEPSEEK_V41_ASSET_DIR")
    study = Path(configured) if configured else ORIGINAL_STUDY
    frozen = study / "formal-summary" / "frozen-summary-inputs.json"
    if not frozen.is_file():
        source = study / "upstream" / "QMSum"
        if not configured:
            download_source_files(source)
        rebuild_frozen_inputs(source, frozen)
    return frozen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, help="Use an existing QMSum tree offline.")
    parser.add_argument(
        "--output",
        type=Path,
        default=ORIGINAL_STUDY / "formal-summary" / "frozen-summary-inputs.json",
        help="Write the frozen input JSON. An existing freeze must match byte for byte.",
    )
    args = parser.parse_args()
    if args.source_root is None:
        source = ORIGINAL_STUDY / "upstream" / "QMSum"
        download_source_files(source)
    else:
        source = args.source_root
    rebuild_frozen_inputs(source, args.output)


if __name__ == "__main__":
    main()
