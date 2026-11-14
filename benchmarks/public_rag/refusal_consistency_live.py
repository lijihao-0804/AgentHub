"""Capture two independently generated first HTTP replies with retrieval fixed to one method."""

import argparse
import hashlib
from contextlib import ExitStack
from pathlib import Path

import httpx

from benchmarks.public_rag.evidence_retention_live import capture_one
from benchmarks.public_rag.qa_first_response_budget import assemble
from benchmarks.public_rag.qa_first_response_cases import read

SCOPES = {"independent": 50, "historical": 2, "historical_vietnam": 1}
ARM = {"baseline": "A", "candidate": "B", "A": "A", "B": "B"}


def run(*, api, scope, state_dir, protocol, freeze, trace, directory, output):
    target = httpx.URL(api)
    if (
        target.scheme != "http"
        or target.host != "127.0.0.1"
        or target.path.rstrip("/") != "/api/v1"
    ):
        raise ValueError("The live comparison requires the isolated loopback API")
    meta = read(freeze)
    if hashlib.sha256(protocol.read_bytes()).hexdigest() != meta["protocol_sha256"]:
        raise ValueError("Protocol differs from the pre-dispatch freeze")
    state = read(state_dir / "state.json")
    dataset = state["datasets"][scope]
    if (
        dataset["snapshot"] != meta["scope_snapshots"][scope]["snapshot"]
        or state["profile"] != meta["profile"]
        or state["model"] != meta["model"]
    ):
        raise ValueError("Workspace snapshot or model profile differs from the execution freeze")
    owner = read(state_dir / "private-login.json")
    mapping = read(state_dir / f"{scope}-chunks.json")
    cases = [c for c in read(protocol)["cases"] if c["scope"] == scope]
    if len(cases) != len({c["id"] for c in cases}) or len(cases) != SCOPES[scope]:
        raise ValueError("Expected the complete frozen scope")
    (directory / "dispatch").mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        clients = {
            arm: stack.enter_context(
                httpx.Client(
                    base_url=api.rstrip("/") + "/",
                    timeout=300,
                    trust_env=False,
                    headers={"x-refusal-consistency-arm": arm},
                )
            )
            for arm in ("A", "B")
        }
        for case in cases:
            arms = [ARM[key] for key in case["arm_order"]]
            if sorted(arms) != ["A", "B"]:
                raise ValueError("A question must have one first request per arm")
            for arm in arms:
                receipt = directory / f"{arm}-{case['id']}.json"
                if receipt.exists():
                    prior = read(receipt)
                    (row,) = prior["rows"]
                    if (
                        prior["protocol_sha256"] != meta["protocol_sha256"]
                        or prior["source"] != meta["source"]
                        or (prior["arm"], prior["snapshot"]) != (arm, dataset["snapshot"])
                        or row["id"] != case["id"]
                        or row["state"] != "returned"
                    ):
                        raise RuntimeError("Cannot resume from a nonmatching first request")
                    continue
                row = capture_one(
                    clients[arm],
                    case=case,
                    arm=arm,
                    state=state,
                    dataset=dataset,
                    mapping=mapping,
                    protocol=protocol,
                    source=meta["source"],
                    trace=trace,
                    directory=directory,
                    owner=owner,
                    phase="live",
                    retrieval_arm="B",
                )
                (variant,) = [e for e in row["events"] if e["kind"] == "qa_variant"]
                if (
                    variant["arm"] != arm
                    or variant["verify_refusal_consistency"] != (arm == "B")
                    or variant["qa_source_sha256_lf"] != meta["qa_source_sha256_lf"][arm]
                ):
                    raise ValueError("HTTP route used the wrong frozen QA implementation")
    assemble(directory, output, protocol, scopes=[scope], arms=("A", "B"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("state-dir", "protocol", "freeze", "trace", "directory", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--scope", required=True, choices=tuple(SCOPES))
    parser.add_argument("--api", required=True)
    run(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
