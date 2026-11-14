"""Blind, score and audit the frozen three-reader confirmation without provider access."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from fractions import Fraction
from pathlib import Path

from benchmarks.public_rag import qa_first_response_review as review
from benchmarks.public_rag import qa_first_response_verify as verify

ARMS = ("A", "B", "C")
PRIMARY = "reserve16"
SOURCE_COMMIT = "273f68383254735657a323748cc2a3c1d34bc37d"
PATCH_SHA = "f35259dd5a8aa1b3e7b9d06e770223cb797b320b31ac66ae604021809e1cf116"
PROMPTS = {
    "original": "9921923abe84909a433d1333667a6cb16f7ba0928080ec0b21136d1b12238499",
    "strict": "14dff03d37e6df47b06af8430061b880cc79379b4a9fe622ec24b0643e8c52a7",
}
QA_PATH = "packages/knowledge/citation_qa.py"
SCIFACT_SHA = "dec31c8182f3d744c7d2c09423756fd1d17cbef75808db13ba01cc0aab4d1ac6"
PROTOCOL_SHA = "d7e18814f84bab06090551bb39d0bc5dc285d52504aa656278dce753e2194766"
SUPPLEMENT_SHA = "2c998a27848d1223b10bf0dbbd478bc41caa4cdb4bf4590ffef2b68da6591d6e"


def source_provenance(directory):
    from benchmarks.public_rag.qa_first_response_replay import SOURCE_FILES

    proof = review.load(directory / "prompt-provenance.json")
    patch = (directory / "prompt.patch").read_bytes().replace(b"\r\n", b"\n")
    verify.require(
        proof["source_commit"] == SOURCE_COMMIT
        and proof["patch_sha256_lf"] == PATCH_SHA == hashlib.sha256(patch).hexdigest(),
        "Frozen source commit or prompt patch",
    )
    # This hash-pinned published patch contains exactly one hunk on the QA reader.
    # Reconstruct it in memory so verification requires only read access to the repository.
    hunk = patch.splitlines(keepends=True)[3:]
    before = b"".join(line[1:] for line in hunk if line[:1] in {b" ", b"-"})
    after = b"".join(line[1:] for line in hunk if line[:1] in {b" ", b"+"})
    files = {
        name: subprocess.check_output(
            ["git", "cat-file", "blob", f"{SOURCE_COMMIT}:{name}"],
            cwd=Path(__file__).resolve().parents[2],
        ).replace(b"\r\n", b"\n")
        for name in SOURCE_FILES
    }
    verify.require(files[QA_PATH].count(before) == 1, "Prompt patch context")
    for variant in ("original", "strict"):
        if variant == "strict":
            files[QA_PATH] = files[QA_PATH].replace(before, after, 1)
        raw_hashes, lf_hashes = {}, {}
        for name, lf in files.items():
            raw = lf.replace(b"\n", b"\r\n") if variant == "original" else lf
            raw_hashes[name] = hashlib.sha256(raw).hexdigest()
            lf_hashes[name] = hashlib.sha256(lf).hexdigest()
        v = proof[variant]
        verify.require(
            v["source_lf_sha256"] == lf_hashes
            and v["source_raw_sha256"] == raw_hashes
            and v["citation_qa_py_sha256_lf"] == lf_hashes[QA_PATH]
            and v["system_prompt_sha256"] == PROMPTS[variant],
            f"{variant}: recorded source differs from frozen Git blobs and patch",
        )
    changed = {
        name
        for name in SOURCE_FILES
        if proof["original"]["source_lf_sha256"][name] != proof["strict"]["source_lf_sha256"][name]
    }
    verify.require(changed == {QA_PATH}, "Unexpected reader source delta")
    return proof


def read_cohort(directory, cases, protocol_sha):
    groups = {arm: {} for arm in ARMS}
    for scope in {c["scope"] for c in cases.values()}:
        subset = {cid for cid, c in cases.items() if c["scope"] == scope}
        for arm in ARMS:
            receipt = review.load(directory / f"{arm}-{scope}.json")
            rows = review.index(receipt["rows"])
            verify.require(
                receipt["arm"] == arm
                and receipt["endpoint"] == "POST /api/v1/knowledge/query"
                and receipt["protocol_sha256"] == protocol_sha
                and set(rows) == subset
                and all(r["state"] == "returned" for r in rows.values()),
                f"Invalid or incomplete {arm}-{scope} first responses",
            )
            groups[arm][scope] = receipt
    auth_coverage(groups)
    return groups


def auth_coverage(groups):
    coverage = {}
    for scope in groups["A"]:
        expired_by_arm = []
        for arm in ARMS:
            rows = review.index(groups[arm][scope]["rows"])
            expired = set()
            for cid, r in rows.items():
                if r["http"] != 401:
                    continue
                verify.require(
                    r["state"] == "returned"
                    and r.get("error") == "ACCESS_TOKEN_EXPIRED"
                    and r.get("message") == "Access token has expired."
                    and r.get("events") == []
                    and r.get("citations", []) == []
                    and r.get("answer") is None
                    and r.get("insufficient") is None,
                    f"{cid}/{arm}: invalid pre-retrieval 401",
                )
                expired.add(cid)
            expired_by_arm.append(expired)
        verify.require(
            expired_by_arm[0] == expired_by_arm[1] == expired_by_arm[2],
            f"{scope}: unmatched authentication failures",
        )
        coverage[scope] = {
            "paired_questions": len(rows),
            "authenticated_pairs": len(rows) - len(expired),
            "pre_retrieval_401_ids": sorted(expired),
        }
    return coverage


def blind(cases, groups):
    return review.packets(
        cases,
        {a: [r for receipt in g.values() for r in receipt["rows"]] for a, g in groups.items()},
    )


def precision(metrics):
    n = metrics["returned_citations"]
    return Fraction(metrics["supporting_citations"], n) if n else None


def comparison(cases, details, old, new):
    pair = {cid: {"baseline": row[old], "candidate": row[new]} for cid, row in details.items()}
    summary = review.summarize(pair, cases)
    outcome = review.decision(summary, PRIMARY)
    fresh = summary[PRIMARY]
    before, after = (fresh["metrics"][which] for which in ("baseline", "candidate"))
    previous_precision, new_precision = precision(before), precision(after)
    benefits = {
        "more_http_200": after["http200"] > before["http200"],
        "more_supported_answers": len(fresh["supported_answer"]["gained"])
        > len(fresh["supported_answer"]["lost"]),
        "more_correct_refusals": len(fresh["correct_refusal"]["gained"])
        > len(fresh["correct_refusal"]["lost"]),
        "fewer_incorrect_positives": len(fresh["wrong_positive"]["lost"])
        > len(fresh["wrong_positive"]["gained"]),
        "higher_citation_precision": previous_precision is not None
        and new_precision is not None
        and new_precision > previous_precision,
    }
    checks = outcome.pop("independent_checks")
    if old == "B" and new == "C":
        del checks["http_200_improved"]
        checks["http_200_not_decreased"] = after["http200"] >= before["http200"]
        checks["observed_benefit_over_B"] = any(benefits.values())
    return {
        "baseline": old,
        "candidate": new,
        "summary": summary,
        **outcome,
        "observed_benefits": benefits,
        "primary_gate_checks": checks,
        "primary_semantic_and_http_gate_met": all(v is True for v in checks.values()),
    }


def report(cases, masked, keys, marks, *, supplement=False):
    verify.require(set(marks) == set(masked), "Incomplete or stale blinded judgments")
    details = {}
    for cid, case in cases.items():
        details[cid] = {}
        for arm in ARMS:
            mid = keys[arm][cid]
            item = masked[mid]
            mark = review.assess(item, marks[mid])
            details[cid][arm] = {
                "scope": case["scope"],
                "masked_id": mid,
                **mark,
                "unsupported_citations": sorted(
                    {c["number"] for c in item["citations"]} - set(mark["supporting_citations"])
                ),
            }
    if supplement:
        return {
            "cohort": "authenticated-supplement",
            "questions": len(cases),
            "rows": details,
            "diagnostic_comparisons": {
                f"{new}-{old}": review.summarize(
                    {
                        cid: {"baseline": row[old], "candidate": row[new]}
                        for cid, row in details.items()
                    },
                    cases,
                )
                for old, new in (("A", "C"), ("B", "C"), ("A", "B"))
            },
        }
    return {
        "primary_cohort": PRIMARY,
        "primary_http_denominator": sum(c["scope"] == PRIMARY for c in cases.values()),
        "primary_authenticated_pairs": sum(
            cases[cid]["scope"] == PRIMARY and all(row[a]["http"] != 401 for a in ARMS)
            for cid, row in details.items()
        ),
        "rows": details,
        "comparisons": {
            f"{new}-{old}": comparison(cases, details, old, new)
            for old, new in (("A", "C"), ("B", "C"), ("A", "B"))
        },
        "interpretation": "Apply the two primary gates together with original-source verification; "
        "inspect the four historical controls separately. B-A is descriptive.",
    }


def audit(cases, groups, corpora, provenance):
    coverage = auth_coverage(groups)
    result = {a: {} for a in ARMS}
    for scope in groups["A"]:
        original = groups["A"][scope]
        for arm in ARMS:
            receipt = groups[arm][scope]
            variant = "strict" if arm == "C" else "original"
            cap = 512 if arm == "A" else 2048
            verify.require(
                receipt["model"] == "deepseek-v4.1-flash-1017"
                and all(
                    receipt[k] == original[k]
                    for k in ("model", "snapshot", "snapshot_hash", "chunks", "documents")
                )
                and receipt["source"] == provenance[variant]["source_raw_sha256"]
                and groups["B"][scope]["profile"] == groups["C"][scope]["profile"]
                and original["profile"] != groups["B"][scope]["profile"],
                f"{scope}/{arm}: source, profile or snapshot drift",
            )
            for cid, row in review.index(receipt["rows"]).items():
                if row["http"] == 401:
                    result[arm][cid] = {
                        "scope": scope,
                        "http": 401,
                        "elapsed_s": row["elapsed_s"],
                        "authenticated": False,
                        "wire_attempts": 0,
                        "responses": [],
                        "adapter_errors": [],
                        "verified_citations": 0,
                        "verified_references": [],
                        "failure_observation": "authentication:ACCESS_TOKEN_EXPIRED",
                        "candidate": {},
                        "reader": {},
                    }
                    continue
                docs = corpora["full" if scope == "full" else "qasper"]
                data = verify.audit_row(cases[cid], row, receipt, docs, cap=cap, thinking=None)
                wire = verify.events_of(row, "wire")
                verify.require(
                    1 <= len(wire) <= 2
                    and all(
                        w["system_prompt_sha256"] == PROMPTS[variant] and w["timeout_seconds"] == 60
                        for w in wire
                    )
                    and len(wire) == len(data["responses"]) + len(data["adapter_errors"]),
                    f"{cid}/{arm}: unexpected wire attempt, timeout or system prompt",
                )
                if arm != "A":
                    a = result["A"][cid]
                    verify.require(
                        data["digest"] == a["digest"] and data["prompt_sha"] == a["prompt_sha"],
                        f"{cid}/{arm}: different evidence sent",
                    )
                result[arm][cid] = {
                    **data,
                    "authenticated": True,
                    "scope": scope,
                    "system_prompt_sha256": PROMPTS[variant],
                    "timeout_seconds": 60,
                    "verified_references": [
                        {
                            k: c[k]
                            for k in (
                                "number",
                                "source",
                                "ordinal",
                                "revision",
                                "chunk_id",
                                "excerpt_start_in_chunk",
                                "excerpt_end_in_chunk",
                                "excerpt_sha256",
                                "chunk_text_sha256",
                                "locator",
                            )
                        }
                        for c in row.get("citations", [])
                    ],
                }
    return {
        "paired_questions": len(cases),
        "auth_coverage_by_scope": coverage,
        "authenticated_pairs": sum(v["authenticated_pairs"] for v in coverage.values()),
        "source_provenance": provenance,
        "rows": result,
        "verified_wire_fields": [
            "model",
            "cap",
            "temperature",
            "timeout",
            "retry_limit",
            "enable_thinking",
            "system_prompt_sha256",
            "user_evidence_sha256",
        ],
        "region": "Wire events omit provider region. See the separate route attestation.",
        "metrics": {
            scope: {
                a: verify.summarize(
                    [r for r in rows.values() if scope == "all" or r["scope"] == scope]
                )
                for a, rows in result.items()
            }
            for scope in ["all", *sorted(groups["A"])]
        },
    }


def supplement_inputs(directory):
    parent = directory.parent
    path = parent / "protocol3.json"
    verify.require(verify.sha(path.read_text(encoding="utf8")) == PROTOCOL_SHA, "Parent protocol")
    proto = review.load(path)
    original_cases = review.index(proto["cases"])
    original = read_cohort(parent, original_cases, PROTOCOL_SHA)
    expired = set(auth_coverage(original)[PRIMARY]["pre_retrieval_401_ids"])
    fresh = directory / "protocol.json"
    verify.require(
        verify.sha(fresh.read_text(encoding="utf8")) == SUPPLEMENT_SHA, "Frozen supplement protocol"
    )
    projection = review.load(fresh)
    cases = review.index(projection["cases"])
    verify.require(
        projection["parent_protocol_sha256"] == PROTOCOL_SHA
        and len(cases) == 4
        and set(cases) == expired
        and all(c == original_cases[k] for k, c in cases.items()),
        "Supplement must contain exactly the original four pre-retrieval 401 cases",
    )
    groups = read_cohort(directory, cases, SUPPLEMENT_SHA)
    frozen = projection["source_system_sha"]
    snapshot = frozen["snapshots"][PRIMARY]
    for arm in ARMS:
        first, followup = original[arm][PRIMARY], groups[arm][PRIMARY]
        variant = "strict" if arm == "C" else "original"
        verify.require(
            all(
                first[k] == followup[k]
                for k in (
                    "model",
                    "profile",
                    "source",
                    "snapshot",
                    "snapshot_hash",
                    "documents",
                    "chunks",
                    "endpoint",
                )
            )
            and followup["source"] == frozen[variant]["source_raw_sha256"]
            and followup["profile"] == frozen["profiles"]["512" if arm == "A" else "2048"]
            and followup["snapshot"] == snapshot["snapshot"]
            and followup["snapshot_hash"] == snapshot["snapshot_sha256"]
            and followup["documents"] == snapshot["count"]
            and all(r["http"] != 401 for r in followup["rows"]),
            f"{arm}: supplement source, route profile, snapshot or authentication",
        )
    return proto, cases, groups


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "directory", type=Path, help="directory containing protocol3.json and A/B/C scope receipts"
    )
    p.add_argument(
        "--supplement",
        action="store_true",
        help="audit the separate authenticated follow-up directory",
    )
    for name in ("packet", "judgments", "report", "verification", "qasper", "scifact"):
        p.add_argument("--" + name, type=Path)
    args = p.parse_args()
    if not (args.packet or args.report or args.verification):
        p.error("request a packet, report, or verification")
    if bool(args.report) != bool(args.judgments):
        p.error("supply both --report and --judgments")
    if bool(args.verification) != bool(args.qasper) or bool(args.verification) != bool(
        args.scifact
    ):
        p.error("supply --verification, --qasper and --scifact together")
    if args.supplement:
        proto, cases, groups = supplement_inputs(args.directory)
    else:
        proto, cases, groups = primary_inputs(args.directory)
    if args.packet or args.report:
        masked, keys = blind(cases, groups)
        if args.packet:
            review.save(args.packet, masked)
        if args.report:
            review.save(
                args.report,
                report(
                    cases, masked, keys, review.load(args.judgments), supplement=args.supplement
                ),
            )
    if args.verification:
        for file, expected in [(args.qasper, proto["train_sha256"]), (args.scifact, SCIFACT_SHA)]:
            verify.require(
                hashlib.sha256(file.read_bytes()).hexdigest() == expected, "Corpus identity"
            )
        parent = args.directory.parent if args.supplement else args.directory
        result = audit(
            cases, groups, verify.sources(args.qasper, args.scifact), source_provenance(parent)
        )
        if args.supplement:
            result["cohort"] = "authenticated-supplement"
        review.save(args.verification, result)


def primary_inputs(directory):
    path = directory / "protocol3.json"
    proto = review.load(path)
    cases = review.index(proto["cases"])
    verify.require(
        sorted(
            (s, sum(c["scope"] == s for c in cases.values()))
            for s in {c["scope"] for c in cases.values()}
        )
        == [("full", 1), ("qasper", 1), ("reserve14", 2), ("reserve16", 16)]
        and sum(bool(c.get("negative")) for c in cases.values() if c["scope"] == PRIMARY) == 6,
        "Unexpected frozen cohort sizes",
    )
    protocol_sha = verify.sha(path.read_text(encoding="utf8"))
    verify.require(protocol_sha == PROTOCOL_SHA, "Frozen reader protocol identity")
    return proto, cases, read_cohort(directory, cases, protocol_sha)


if __name__ == "__main__":
    main()
