"""Freeze and verify the two-arm answer-layer study, without any provider access."""

import hashlib
import json
import platform
import re
import subprocess
from collections import Counter
from importlib.metadata import version

from benchmarks.public_rag.confirmation_cases import ROOT, save
from benchmarks.public_rag.confirmation_meter import INPUT, MODEL, OUTPUT, PER_REQUEST

BASE = "de45ac6cb3618333b39c1939803c984292477f56"
OUT = ROOT / "benchmarks/public_rag/results/published/rag-answer-contracts-v1"
LIMITS = {"citation": (32, 1), "support": (128, 4)}
CAP = 160 * PER_REQUEST
ARMS = ("baseline", "candidate")
RUN_DIR = ROOT / ".scratch/rag-answer-contracts-v1-paid"
DATA = ("citation-cases.json", "support-cases.json", "citation-contexts.json", "protocol.md")
CODE = (
    "apps",
    "packages",
    "migrations",
    "scripts",
    "benchmarks/evaluation/experiments/support_refund_rag",
    "benchmarks/public_rag",
    "pyproject.toml",
    "uv.lock",
)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def file_sha(path):
    return sha(path.read_bytes().replace(b"\r\n", b"\n"))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf8").strip()


def runtime():
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        **{
            k: version(k)
            for k in (
                "torch",
                "transformers",
                "sentence-transformers",
                "tokenizers",
                "openai",
                "litellm",
                "sqlalchemy",
                "langgraph",
                "qdrant-client",
            )
        },
    }


def load(path):
    return json.loads(path.read_text(encoding="utf8"))


def cases():
    data = {chain: load(OUT / f"{chain}-cases.json")["cases"] for chain in LIMITS}
    contexts = load(OUT / "citation-contexts.json")
    ids = []
    for chain, group in data.items():
        counts = Counter(c["stratum"] for c in group)
        expected = (
            {"complete": 6, "insufficient": 6, "partial": 4}
            if chain == "citation"
            else {"observation": 4, "fields": 4, "policy": 4, "capability": 4}
        )
        if counts != expected:
            raise ValueError(f"Frozen {chain} strata differ")
        for c in group:
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]+", c["case_id"])
                or not c["input"].strip()
                or not c["rubric"]
                or not c["documents"]
                or type(c["should_withhold"]) is not bool
            ):
                raise ValueError("Missing case, reference or preregistered rubric")
            if chain == "citation" and c["availability"] not in {
                "visible",
                "source_only",
                "unanswerable",
                "partial",
            }:
                raise ValueError("Missing source/visible distinction")
            if chain == "support":
                from benchmarks.evaluation.experiments.support_refund_rag import (
                    answer_contract_pair,
                )

                answer_contract_pair.validate(c)
            ids.append(c["case_id"])
    if len(ids) != len(set(ids)) or set(contexts) != {c["case_id"] for c in data["citation"]}:
        raise ValueError("Duplicate case or missing frozen context")
    for i in range(16):
        for j, (chain, group) in enumerate(data.items()):
            c = group[i]
            for arm in ARMS[:: 1 if (i + j) % 2 == 0 else -1]:
                yield chain, c, arm


def check_code(commit, *, tracked_only=False):
    # The report and sample commit follows the candidate. Only executable changes
    # and protected fixture/config changes invalidate the comparison.
    excludes = [":(exclude)benchmarks/public_rag/results"]
    paths = [*CODE, *excludes]
    if git("diff", "--name-only", commit, "--", *paths):
        raise ValueError("Candidate or runtime inputs changed")
    if not tracked_only and git("ls-files", "--others", "--exclude-standard", "--", *paths):
        raise ValueError("Unexpected untracked implementation or fixture")


def manifest(commit, evaluator):
    if (
        git("rev-parse", commit) != commit
        or git("merge-base", BASE, commit) != BASE
        or git("rev-parse", evaluator) != evaluator
        or git("merge-base", commit, evaluator) != commit
    ):
        raise ValueError("Candidate must descend from the reviewed base")
    # Independent samples were authored after the product and dispatcher froze.
    # Only the read-only report/export and its manifest checker may differ.
    allowed = {
        "benchmarks/public_rag/answer_pair_score.py",
        "benchmarks/public_rag/answer_pair_freeze.py",
    }
    changed = git(
        "diff",
        "--name-only",
        commit,
        evaluator,
        "--",
        *CODE,
        ":(exclude)benchmarks/public_rag/results",
    ).splitlines()
    if set(changed) - allowed:
        raise ValueError("Product, dispatch or other study inputs changed after candidate freeze")
    check_code(evaluator)
    schedule = [f"{chain}/{c['case_id']}/{arm}" for chain, c, arm in cases()]
    return {
        "baseline_commit": BASE,
        "candidate_commit": commit,
        "reporting_commit": evaluator,
        "environment": runtime(),
        "files": {name: file_sha(OUT / name) for name in DATA},
        "schedule": schedule,
        "model": MODEL,
        "region": "Beijing",
        "max_requests": 160,
        "cap_microcny": CAP,
        "input_token_reserve": 160 * INPUT,
        "output_token_reserve": 160 * OUTPUT,
        "temperature": 0,
        "citation_thinking": False,
        "support_thinking": "frozen provider tool mode",
        "case_request_caps": {k: v[1] for k, v in LIMITS.items()},
    }


def freeze(commit):
    save(OUT / "freeze.json", manifest(commit, git("rev-parse", "HEAD")))


def verify():
    f = load(OUT / "freeze.json")
    if f != manifest(f["candidate_commit"], f["reporting_commit"]):
        raise ValueError("Study manifest or environment drift")
    return f
